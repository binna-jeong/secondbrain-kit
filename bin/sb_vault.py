#!/usr/bin/env python3
"""세컨브레인 볼트 구성: 쌓인 기록을 읽어 프로젝트(scope)·별칭·레이어 대상을 제안하고, 승인한 것만 적용한다.

  sb vault analyze [--json]                  기록 분석(읽기 전용) → $SB_HOME/vault/analysis-<시각>.json
  sb vault plan [--analysis F] [--json]      분석으로 구성안 → $SB_HOME/vault/plan-<시각>.json + 요약
  sb vault apply --plan F [--yes --hash H]   구성안 적용(백업 → 쓰기 → 검증 → 실패 시 자동 되돌리기)
  sb vault rollback [--apply F]              마지막(또는 지정한) 적용을 되돌린다
  sb vault status [--json]                   데이터 양·현재 볼트·마지막 적용

원칙
- 분석은 이 PC 안에서만 한다. 네트워크는 사용자가 설정한 LLM 주소(선택)뿐이고, SB_VAULT_LLM=off 면 쓰지 않는다.
  LLM 에는 폴더 이름 조각만 보낸다(대화 내용·관측 본문은 보내지 않는다).
- 분류 체계를 새로 만들지 않는다. 관측 type 분포와 폴더·git·자동 메모리 신호만 쓴다.
- 적용은 기존 값을 덮어쓰지 않는다. 같은 키가 다른 값이면 기존 값을 두고 conflicts 로 보고한다.
- 수집 제외(claude-mem CLAUDE_MEM_EXCLUDED_PROJECTS)는 보고만 한다 — 바꾸면 기록이 사라진다.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import sb_config  # noqa: E402
import sb_scope  # noqa: E402
import sync_automemory  # noqa: E402

SCHEMA = 1
# 데이터 양 문턱 — 운영본 실측(11-15): 하루 관측 2천~1만8천 건이라 관측 수만으로는 하루 만에 넘는다.
# 그래서 홈·임시를 뺀 작업 폴더 수를 같이 본다(M1 baseline_vault.md §1-4).
ENOUGH_OBS, ENOUGH_FOLDERS, ENOUGH_SESSIONS = 500, 3, 30
MIN_SCOPE_OBS, MIN_SCOPE_SESSIONS = 20, 3     # 이보다 적은 폴더는 scope 로 제안하지 않는다(보고만)
ACTIVE_DAYS = 30                              # 최근 이 기간에 기록이 있어야 상태층 쓰기 대상으로 제안
GENERIC_NAMES = {'src', 'app', 'apps', 'code', 'project', 'projects', 'workspace', 'work', 'repo', 'repos', 'tmp',
                 'test', 'tests', 'dev', 'main', 'new', 'old', 'backup', 'output', 'build', 'dist', 'docs'}
STATE_ENV = 'SB_STATE_PILOT_SCOPES'


# ---------------------------------------------------------------- 경로·시각

def vault_dir() -> Path:
    return sb_config.home() / 'vault'


def vault_json_path() -> Path:
    return sb_config.home() / 'config' / 'vault.json'


def loops_path() -> Path:
    return Path(os.environ.get('SB_LOOPS_PATH') or sb_config.sb_path('loops', 'loops.jsonl'))


def claude_settings_path() -> Path:
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or '~/.claude').expanduser() / 'settings.json'


def stamp() -> str:
    return time.strftime('%Y%m%d-%H%M%S')


def now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S%z')


def _home() -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser('~')))


def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(p)))


def _short(p: str) -> str:
    """보고용 경로 — 홈은 ~ 로 줄인다."""
    h = os.path.expanduser('~')
    return '~' + p[len(h):] if p == h or p.startswith(h + os.sep) else p


def classify_path(p: str) -> str:
    """project | home | temp | automation. 홈·시스템 뿌리·임시·에이전트 내부 폴더는 scope 로 쓰지 않는다."""
    real = _norm(p)
    home = _home()
    if real == home or real in (os.path.normcase(os.path.realpath(os.sep)),) or len(real) <= 3:
        return 'home'
    for d in ('.claude', '.claude-mem', '.codex', '.gemini', '.omo', '.cache', 'Library'):
        if sb_scope.path_within(real, os.path.join(home, d)):
            return 'automation'
    if sb_scope.path_within(real, home):
        return 'project'
    if any(sb_scope.path_within(real, _norm(r)) for r in sync_automemory.temp_roots()):
        return 'temp'
    return 'automation'   # 홈 밖(시스템·다른 볼륨) — 보통 도구가 연 폴더다


def git_root(p: str) -> Optional[str]:
    try:
        r = subprocess.run(['git', '-C', p, 'rev-parse', '--show-toplevel'], capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return os.path.realpath(out) if r.returncode == 0 and out else None


# ---------------------------------------------------------------- 원자료 읽기(읽기 전용)

def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect('file:%s?mode=ro' % Path(path).as_posix(), uri=True, timeout=10)


def read_claude_mem(db_path: Optional[str] = None) -> Dict[str, Any]:
    """세션 cwd·관측 꼬리표·관측 type 분포. DB 가 없으면 빈 결과."""
    db_path = db_path or sb_config.claude_mem_db()
    out: Dict[str, Any] = {'readable': False, 'sessions': [], 'tags': {}, 'types': {}, 'obs_total': 0,
                           'session_total': 0, 'span': [None, None]}
    if not Path(db_path).is_file():
        out['reason'] = 'claude-mem.db 없음'
        return out
    try:
        with closing(_ro(db_path)) as db:
            cols = {r[1] for r in db.execute('PRAGMA table_info(sdk_sessions)')}
            cwd_col = 'cwd' if 'cwd' in cols else 'NULL'
            per_session: Dict[str, Tuple[int, int]] = {}
            for sid, n, last in db.execute('SELECT memory_session_id, COUNT(*), MAX(created_at_epoch) '
                                           'FROM observations GROUP BY memory_session_id'):
                per_session[sid] = (n, last or 0)
            for sid, project, cwd, started in db.execute(
                    'SELECT memory_session_id, project, %s, started_at_epoch FROM sdk_sessions' % cwd_col):
                n, last = per_session.get(sid, (0, 0))
                out['sessions'].append({'project': project, 'cwd': cwd, 'obs': n, 'last': max(last, started or 0)})
            for tag, n, last in db.execute('SELECT project, COUNT(*), MAX(created_at_epoch) FROM observations GROUP BY project'):
                out['tags'][tag] = {'obs': n, 'last': last or 0}
            out['types'] = {t: n for t, n in db.execute('SELECT type, COUNT(*) FROM observations GROUP BY type ORDER BY 2 DESC')}
            lo, hi = db.execute('SELECT MIN(created_at_epoch), MAX(created_at_epoch) FROM observations').fetchone()
            out['span'] = [lo, hi]
    except sqlite3.Error as e:
        out['reason'] = 'claude-mem.db 읽기 실패: %s' % e
        return out
    out['readable'] = True
    out['obs_total'] = sum(v['obs'] for v in out['tags'].values())
    out['session_total'] = len(out['sessions'])
    return out


def read_project_dirs(root: Optional[str] = None) -> List[Dict[str, Any]]:
    """Claude Code 프로젝트 폴더별 (작업 폴더, 자동 메모리 수, feedback 수). 임시 폴더 슬러그는 건너뛴다."""
    import sb_rules
    base = sync_automemory.projects_root(root)
    out = []
    for d in sorted(base.glob('*')) if base.is_dir() else []:
        if not d.is_dir() or sync_automemory.is_temp_slug(d.name):
            continue
        mem = d / 'memory'
        files = [p for p in mem.glob('*.md') if p.name != 'MEMORY.md'] if mem.is_dir() else []
        feedback = 0
        for p in files:
            try:
                head = p.read_text(encoding='utf-8', errors='replace')[:600]
            except OSError:
                continue
            if re.search(r'^\s*type:\s*feedback\b', head, re.M):
                feedback += 1
        cwd = sb_rules.session_cwd(d)
        out.append({'slug': d.name, 'cwd': cwd, 'memories': len(files), 'feedback': feedback,
                    'name': sync_automemory.project_from_slug(d.name)})
    return out


def capture_excluded() -> List[str]:
    raw = (sb_config.claude_mem_settings() or {}).get('CLAUDE_MEM_EXCLUDED_PROJECTS') or ''
    return [x.strip() for x in str(raw).split(',') if x.strip()]


# ---------------------------------------------------------------- 분석

def _suggest_key(root: str, taken: Dict[str, str]) -> str:
    base = os.path.basename(root.rstrip(os.sep)) or root
    key = base
    if key.lower() in GENERIC_NAMES or (key in taken and taken[key] != root):
        parent = os.path.basename(os.path.dirname(root.rstrip(os.sep)))
        key = '%s-%s' % (parent, base) if parent else base
    return key


def analyze(db_path: Optional[str] = None, projects_root: Optional[str] = None, use_git: bool = True,
            llm: Optional[str] = None) -> Dict[str, Any]:
    cm = read_claude_mem(db_path)
    dirs = read_project_dirs(projects_root)
    aliases = sb_scope._load_aliases()
    alias_paths = [(_norm(e), k) for k, v in aliases.items() for e in v if sb_scope.is_path_entry(e)]

    def alias_of(p: str) -> Optional[str]:
        real = _norm(p)
        best = None
        for ap, k in alias_paths:
            if sb_scope.path_within(real, ap) and (best is None or len(ap) > len(best[0])):
                best = (ap, k)
        return best[1] if best else None

    # 1) 작업 폴더별 집계
    folders: Dict[str, Dict[str, Any]] = defaultdict(lambda: {'obs': 0, 'sessions': 0, 'last': 0, 'tags': defaultdict(int),
                                                            'memories': 0, 'feedback': 0, 'signals': set()})
    for s in cm['sessions']:
        if not s['cwd']:
            continue
        f = folders[os.path.realpath(s['cwd'])]
        f['obs'] += s['obs']
        f['sessions'] += 1
        f['last'] = max(f['last'], s['last'])
        f['tags'][s['project']] += s['obs']
        f['signals'].add('session_cwd')
    for d in dirs:
        if not d['cwd']:
            continue
        f = folders[os.path.realpath(d['cwd'])]
        f['memories'] += d['memories']
        f['feedback'] += d['feedback']
        f['signals'].add('memory_dir')
    # 꼬리표만 있고 cwd 가 없는 관측(옛 세션)은 이름이 같은 폴더에 붙인다: 홈 바로 아래 같은 이름 폴더,
    # 없으면 이미 아는 폴더 중 이름이 같은 것이 하나뿐일 때 그 폴더. 둘 다 없으면 보고만 한다.
    known_tags = {t for f in folders.values() for t in f['tags']}
    home = os.path.expanduser('~')
    by_base: Dict[str, List[str]] = defaultdict(list)
    for fp in list(folders):
        by_base[os.path.basename(fp.rstrip(os.sep))].append(fp)
    unmapped = {}
    for tag, v in cm['tags'].items():
        if tag in known_tags or not tag or os.sep in tag:
            if tag not in known_tags:
                unmapped[tag] = v['obs']
            continue
        same = by_base.get(tag, [])
        # 홈 바로 아래 같은 이름 폴더가 먼저다(워커가 cwd 이름으로 꼬리표를 단 옛 세션 대부분이 여기다)
        cand = os.path.join(home, tag) if os.path.isdir(os.path.join(home, tag)) else same[0] if len(same) == 1 else None
        if not cand:
            unmapped[tag] = v['obs']
            continue
        f = folders[os.path.realpath(cand)]
        f['obs'] += v['obs']
        f['last'] = max(f['last'], v['last'])
        f['tags'][tag] += v['obs']
        f['signals'].add('tag_name')

    # 2) 폴더 → 프로젝트 뿌리: 기존 별칭 > git 최상위 > 이미 뿌리인 상위 폴더 > 자기 자신
    classes = {p: classify_path(p) for p in folders}
    roots: Dict[str, Tuple[str, str]] = {}   # 폴더 → (뿌리, 방법)
    for p in folders:
        if classes[p] != 'project':
            continue
        key = alias_of(p)
        if key:
            roots[p] = ('alias:' + key, 'alias')
            continue
        g = git_root(p) if use_git else None
        base = os.path.basename(p.rstrip(os.sep))
        # 상위 저장소 안이어도 자기 이름 꼬리표로 관측이 충분히 쌓인 폴더는 따로 선 프로젝트다
        # (운영본 실측: 플러그인 저장소 안 플러그인, 워크스페이스 공장 산출물 폴더)
        if g and _norm(g) != _norm(p) and folders[p]['tags'].get(base, 0) >= MIN_SCOPE_OBS:
            roots[p] = (p, 'own_tag')
        elif g and classify_path(g) == 'project':
            roots[p] = (g, 'git')
    project_folders = sorted((p for p in folders if classes[p] == 'project' and p not in roots), key=len)
    for p in project_folders:
        parent = next((r for r in project_folders if r != p and sb_scope.path_within(_norm(p), _norm(r))
                       and r not in roots), None)
        roots[p] = (roots[parent][0], 'parent') if parent and parent in roots else (p, 'folder')

    # 3) 뿌리별 후보
    groups: Dict[str, Dict[str, Any]] = {}
    for p, (root, method) in roots.items():
        g = groups.setdefault(root, {'paths': [], 'obs': 0, 'sessions': 0, 'last': 0, 'tags': defaultdict(int),
                                     'memories': 0, 'feedback': 0, 'signals': set()})
        f = folders[p]
        g['paths'].append(p)
        for k in ('obs', 'sessions', 'memories', 'feedback'):
            g[k] += f[k]
        g['last'] = max(g['last'], f['last'])
        for t, n in f['tags'].items():
            g['tags'][t] += n
        g['signals'] |= f['signals'] | {method}
    taken: Dict[str, str] = {}
    projects = []
    for root, g in sorted(groups.items(), key=lambda kv: -kv[1]['obs']):
        if root.startswith('alias:'):
            key = root.split(':', 1)[1]
            real_root = next((e for e in aliases.get(key, []) if sb_scope.is_path_entry(e)), g['paths'][0])
            conf = 1.0
        else:
            real_root = root
            key = _suggest_key(root, taken)
            conf = 1.0 if 'git' in g['signals'] else 0.7
        taken.setdefault(key, real_root)
        projects.append({
            'scope_id': sb_scope.resolve_scope_id(real_root, use_git=use_git)[0] if os.path.isdir(real_root) else key,
            'suggested_key': key, 'root': real_root, 'paths': sorted(g['paths'], key=len)[:20],
            'path_count': len(g['paths']), 'obs_count': g['obs'], 'session_count': g['sessions'],
            'last_seen': time.strftime('%Y-%m-%d', time.localtime(g['last'] / 1000)) if g['last'] else None,
            'last_epoch_ms': g['last'], 'memories': g['memories'], 'feedback': g['feedback'],
            'observed_tags': sorted(g['tags'], key=lambda t: -g['tags'][t])[:10],
            'signals': sorted(g['signals']), 'class': 'project', 'confidence': conf,
            'existing_alias': root.startswith('alias:'),
        })
    excluded = []
    for p, c in classes.items():
        if c != 'project':
            f = folders[p]
            excluded.append({'path': p, 'class': c, 'obs_count': f['obs'], 'session_count': f['sessions']})
    excluded.sort(key=lambda x: -x['obs_count'])

    llm_info = {'used': False, 'route': None, 'calls': 0}
    if llm_mode(llm) != 'off':
        llm_info = refine_names(projects, llm)

    work_folders = sum(1 for p in projects if p['obs_count'] >= MIN_SCOPE_OBS or p['session_count'] >= MIN_SCOPE_SESSIONS)
    obs, sess = cm['obs_total'], cm['session_total']
    volume = 'empty' if obs == 0 and sess == 0 and not dirs else \
        'enough' if (obs >= ENOUGH_OBS and work_folders >= ENOUGH_FOLDERS) or sess >= ENOUGH_SESSIONS else 'thin'
    return {
        'version': SCHEMA, 'created_at': now_iso(),
        'sources': [
            {'name': 'claude-mem', 'path': _short(db_path or sb_config.claude_mem_db()), 'readable': cm['readable'],
             'count': obs, 'sessions': sess, 'reason': cm.get('reason'),
             'span': [time.strftime('%Y-%m-%d', time.localtime(x / 1000)) if x else None for x in cm['span']]},
            {'name': 'claude-projects', 'path': _short(str(sync_automemory.projects_root(projects_root))),
             'readable': bool(dirs), 'count': len(dirs),
             'memories': sum(d['memories'] for d in dirs), 'feedback': sum(d['feedback'] for d in dirs)},
        ],
        'projects': projects, 'excluded': excluded[:200], 'excluded_count': len(excluded),
        'unmapped_tags': dict(sorted(unmapped.items(), key=lambda kv: -kv[1])[:30]),
        'kinds': dict(list(cm['types'].items())[:12]),
        'kinds_other': max(0, len(cm['types']) - 12),
        'rule_candidates': sum(d['feedback'] for d in dirs),
        'capture_excluded': capture_excluded(),
        'existing_aliases': len(aliases),
        'llm': llm_info, 'data_volume': volume, 'work_folders': work_folders,
    }


# ---------------------------------------------------------------- LLM(선택)

def llm_mode(value: Optional[str] = None) -> str:
    v = (value or os.environ.get('SB_VAULT_LLM') or 'auto').strip().lower()
    return 'off' if v in ('off', '0', 'false', 'no') else 'auto'


def llm_route() -> Optional[Tuple[str, str, str]]:
    """(url, model, key). 키트 공통 LLM 설정을 재사용한다: SB_VAULT_LLM_* > SB_CORRECTION_LLM_*."""
    for prefix in ('SB_VAULT_LLM', 'SB_CORRECTION_LLM'):
        url = os.environ.get(prefix + '_URL')
        model = os.environ.get(prefix.replace('_LLM', '') + '_MODEL') or os.environ.get(prefix + '_MODEL')
        if url and model:
            return url, model, os.environ.get(prefix + '_KEY') or ''
    return None


def needs_name(p: Dict[str, Any]) -> bool:
    base = os.path.basename(p['root'].rstrip(os.sep)).lower()
    return not p['existing_alias'] and (base in GENERIC_NAMES or bool(re.fullmatch(r'[\d._-]+', base))
                                        or p['suggested_key'] != os.path.basename(p['root'].rstrip(os.sep)))


def refine_names(projects: List[Dict[str, Any]], llm: Optional[str] = None) -> Dict[str, Any]:
    """이름이 애매한 후보(흔한 폴더 이름·충돌)만 LLM 에 짧은 이름을 묻는다. 경로 조각만 보낸다."""
    route = llm_route()
    info = {'used': False, 'route': None, 'calls': 0}
    if not route:
        return info
    url, model, key = route
    todo = [p for p in projects if needs_name(p)][:20]
    if not todo:
        return info
    info['route'] = re.sub(r'^(https?://[^/]+).*$', r'\1', url)
    items = [{'i': i, 'path_parts': [x for x in _short(p['root']).split(os.sep) if x][-3:]} for i, p in enumerate(todo)]
    prompt = ('다음 작업 폴더마다 프로젝트를 가리키는 짧은 영문 소문자 kebab-case 이름 하나를 정해 JSON 배열 '
              '[{"i":번호,"name":"이름"}] 로만 답하라.\n' + json.dumps(items, ensure_ascii=False))
    body = json.dumps({'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'temperature': 0}).encode()
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json',
                                                          **({'Authorization': 'Bearer ' + key} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            text = json.loads(r.read().decode('utf-8'))['choices'][0]['message']['content']
        info['calls'] = 1
        names = json.loads(re.search(r'\[.*\]', text, re.S).group(0))
    except Exception as e:  # noqa: BLE001
        info['error'] = type(e).__name__
        return info
    used = {p['suggested_key'] for p in projects}
    for n in names if isinstance(names, list) else []:
        try:
            p, name = todo[int(n['i'])], str(n['name']).strip()
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        if re.fullmatch(r'[a-z0-9][a-z0-9_-]{1,40}', name) and name not in used:
            used.add(name)
            p['suggested_key'], p['confidence'] = name, 0.6
            p['signals'] = sorted(set(p['signals']) | {'llm_label'})
    info['used'] = True
    return info


# ---------------------------------------------------------------- 구성안

def plan_hash(plan: Dict[str, Any]) -> str:
    body = {k: v for k, v in plan.items() if k not in ('plan_hash', 'created_at')}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()


def read_json_file(p: Path) -> Any:
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def current_env_value(name: str) -> Optional[str]:
    s = read_json_file(claude_settings_path()) or {}
    v = (s.get('env') or {}).get(name)
    return v if isinstance(v, str) else None


def make_plan(analysis: Dict[str, Any], analysis_ref: Optional[str] = None, state_write_mode: str = 'all',
              now_ms: Optional[int] = None) -> Dict[str, Any]:
    now_ms = now_ms or int(time.time() * 1000)
    active_ms = ACTIVE_DAYS * 86400 * 1000
    aliases = sb_scope._load_aliases()
    alias_path_owner = {_norm(e): k for k, v in aliases.items() for e in v if sb_scope.is_path_entry(e)}
    scopes, new_aliases, conflicts, minor = [], {}, [], []
    for p in analysis.get('projects', []):
        if p['obs_count'] < MIN_SCOPE_OBS and p['session_count'] < MIN_SCOPE_SESSIONS:
            minor.append(p['suggested_key'])
            continue
        key = p['suggested_key']
        active = bool(p.get('last_epoch_ms')) and now_ms - p['last_epoch_ms'] <= active_ms
        scopes.append({'key': key, 'root': p['root'], 'paths': [p['root']], 'obs_count': p['obs_count'],
                       'state_write': active, 'rules': p.get('feedback', 0) > 0, 'loops': True,
                       'last_seen': p.get('last_seen'), 'existing_alias': p.get('existing_alias', False)})
        if p.get('existing_alias'):
            continue
        owner = alias_path_owner.get(_norm(p['root']))
        if key in aliases:
            paths = [_norm(e) for e in aliases[key] if sb_scope.is_path_entry(e)]
            if _norm(p['root']) not in paths:
                conflicts.append({'target': 'project_aliases', 'key': key, 'existing': aliases[key], 'proposed': [p['root']]})
            continue
        if owner and owner != key:
            conflicts.append({'target': 'project_aliases', 'key': key, 'existing': {owner: p['root']}, 'proposed': [p['root']]})
            continue
        resolved = sb_scope.resolve_scope_id(p['root'], use_git=True)[0] if os.path.isdir(p['root']) else None
        if resolved != key:
            new_aliases[key] = [p['root']]
    state_list = sorted(s['key'] for s in scopes if s['state_write'])
    env_cur = current_env_value(STATE_ENV)
    env_new = ','.join(state_list) if state_write_mode == 'listed' and state_list else None
    if env_new is not None and env_cur and set(env_cur.split(',')) != set(state_list):
        conflicts.append({'target': 'settings.env.' + STATE_ENV, 'key': STATE_ENV, 'existing': env_cur, 'proposed': env_new})
        env_new = None
    plan = {
        'version': SCHEMA, 'created_at': now_iso(), 'analysis_ref': analysis_ref,
        'data_volume': analysis.get('data_volume'),
        'aliases_path': sb_scope.aliases_path(),
        'scopes': scopes, 'aliases': new_aliases,
        'kind_policy': {'default': 'normal'},
        'state_write_mode': state_write_mode,
        'state_env': env_new,
        'scope_excluded': sorted({_short(e['path']) for e in analysis.get('excluded', []) if e['class'] in ('home', 'temp')}
                                 | {'~', '/tmp', '/private/tmp', '/private/var/folders'}),
        'capture_excluded': analysis.get('capture_excluded', []),
        'minor_folders': minor[:50], 'conflicts': conflicts,
    }
    plan['plan_hash'] = plan_hash(plan)
    return plan


def render_plan(plan: Dict[str, Any]) -> str:
    lines = ['볼트 구성안 (데이터 양: %s, 해시 %s)' % (plan.get('data_volume'), plan['plan_hash'][:12]), '']
    lines.append('프로젝트 %d개 (상태층 쓰기 대상 %d개, 규칙층 대상 %d개)' % (
        len(plan['scopes']), sum(s['state_write'] for s in plan['scopes']), sum(s['rules'] for s in plan['scopes'])))
    for s in plan['scopes'][:30]:
        lines.append('  %-28s 관측 %6d  최근 %s  %s%s' % (
            s['key'], s['obs_count'], s.get('last_seen') or '-', '쓰기' if s['state_write'] else '읽기',
            '' if s.get('existing_alias') else '  (새 이름)' if s['key'] in plan['aliases'] else ''))
    if len(plan['scopes']) > 30:
        lines.append('  … %d개 더' % (len(plan['scopes']) - 30))
    lines.append('')
    lines.append('새 별칭 %d개 → %s' % (len(plan['aliases']), _short(plan['aliases_path'])))
    if plan['state_env']:
        lines.append('%s = %s' % (STATE_ENV, plan['state_env']))
    else:
        lines.append('%s: 바꾸지 않음(%s)' % (STATE_ENV, '전 scope 쓰기 허용 유지' if plan['state_write_mode'] == 'all' else '충돌 또는 대상 없음'))
    lines.append('scope 제외: ' + ', '.join(plan['scope_excluded'][:8]))
    if plan['capture_excluded']:
        lines.append('수집 제외(보고만, 바꾸지 않음): ' + ', '.join(plan['capture_excluded'][:8]))
    if plan['conflicts']:
        lines.append('충돌 %d건 — 기존 값을 유지한다:' % len(plan['conflicts']))
        for c in plan['conflicts'][:10]:
            lines.append('  %s %s: 기존 %s / 제안 %s' % (c['target'], c['key'], c['existing'], c['proposed']))
    return '\n'.join(lines)


# ---------------------------------------------------------------- 적용·되돌리기

def _sha(p: Path) -> Optional[str]:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _atomic_write(p: Path, data: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.sbvault-tmp')
    tmp.write_text(data, encoding='utf-8')
    os.replace(tmp, p)


def _targets(plan: Dict[str, Any]) -> Dict[str, Path]:
    t = {'vault_json': vault_json_path(), 'aliases': Path(plan.get('aliases_path') or sb_scope.aliases_path()),
         'loops': loops_path()}
    if plan.get('state_env'):
        t['settings'] = claude_settings_path()
    return t


def verify_applied(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    checks = []
    v = read_json_file(vault_json_path())
    checks.append({'check': 'vault.json', 'ok': isinstance(v, dict) and v.get('plan_hash') == plan['plan_hash']})
    path = plan.get('aliases_path') or sb_scope.aliases_path()
    table = sb_scope._load_aliases(path)
    for key, paths in plan['aliases'].items():
        got = sb_scope.resolve_scope_id(paths[0], aliases=table, use_git=False)[0] if os.path.isdir(paths[0]) else key
        checks.append({'check': 'alias ' + key, 'ok': got == key})
    if plan.get('state_env'):
        checks.append({'check': STATE_ENV, 'ok': current_env_value(STATE_ENV) == plan['state_env']})
    checks.append({'check': 'loops.jsonl', 'ok': loops_path().is_file()})
    return checks


def apply_plan(plan: Dict[str, Any], approved_hash: str, approved_by: str = 'user_cli',
               _fail_after_write: bool = False) -> Dict[str, Any]:
    if plan_hash(plan) != plan.get('plan_hash'):
        raise SystemExit('구성안 파일이 만든 뒤 바뀌었습니다(plan_hash 불일치) — sb vault plan 을 다시 돌리세요')
    if not approved_hash or not plan['plan_hash'].startswith(approved_hash):
        raise SystemExit('승인한 해시와 구성안이 다릅니다 — 요약을 다시 보고 --hash 로 승인하세요')
    ts = stamp()
    bdir = vault_dir() / 'backup' / ts
    bdir.mkdir(parents=True, exist_ok=True)
    targets = _targets(plan)
    record: Dict[str, Any] = {'version': SCHEMA, 'plan_hash': plan['plan_hash'], 'approved_by': approved_by,
                              'applied_at': now_iso(), 'backups': [], 'writes': [], 'rolled_back': False}
    for name, p in targets.items():
        before = _sha(p)
        bpath = None
        if before is not None:
            bpath = bdir / (name + '.bak')
            shutil.copy2(p, bpath)
        record['backups'].append({'target': str(p), 'backup_path': str(bpath) if bpath else None, 'sha256_before': before})
    apply_path = vault_dir() / ('apply-%s.json' % ts)
    try:
        # 1) 별칭 병합 — 기존 키·경로는 건드리지 않는다
        ap = targets['aliases']
        cur = read_json_file(ap) if ap.exists() else {}
        if not isinstance(cur, dict):
            raise ValueError('별칭 파일이 JSON 객체가 아님: %s' % ap)
        for k, v in plan['aliases'].items():
            if k not in cur:
                cur[k] = v
        _atomic_write(ap, json.dumps(cur, ensure_ascii=False, indent=1) + '\n')
        # 2) vault.json(정본)
        _atomic_write(targets['vault_json'], json.dumps({
            'version': SCHEMA,
            'scopes': {s['key']: {'root': s['root'], 'state_write': s['state_write'], 'rules': s['rules'], 'loops': s['loops']}
                       for s in plan['scopes']},
            'kind_policy': plan['kind_policy'], 'state_write_mode': plan['state_write_mode'],
            'scope_excluded': plan['scope_excluded'], 'updated_at': now_iso(), 'plan_hash': plan['plan_hash'],
        }, ensure_ascii=False, indent=1) + '\n')
        # 3) 미결 원장 — 없을 때만 빈 파일
        if not targets['loops'].exists():
            _atomic_write(targets['loops'], '')
        # 4) 상태층 쓰기 허용 env — listed 모드이고 기존 값이 없을 때만(make_plan 이 충돌을 걸렀다)
        if 'settings' in targets:
            s = read_json_file(targets['settings']) or {}
            env = s.setdefault('env', {})
            if env.get(STATE_ENV) in (None, '', plan['state_env']):
                env[STATE_ENV] = plan['state_env']
            _atomic_write(targets['settings'], json.dumps(s, ensure_ascii=False, indent=2) + '\n')
        for name, p in targets.items():
            record['writes'].append({'target': str(p), 'sha256_after': _sha(p)})
        checks = verify_applied(plan)
        if _fail_after_write:
            checks.append({'check': 'forced_failure', 'ok': False})
        record['verify'] = {'ok': all(c['ok'] for c in checks), 'checks': checks}
    except Exception as e:  # noqa: BLE001
        record['verify'] = {'ok': False, 'checks': [{'check': 'exception', 'ok': False, 'error': str(e)[:200]}]}
    _atomic_write(apply_path, json.dumps(record, ensure_ascii=False, indent=1))
    if not record['verify']['ok']:
        rollback(apply_path)
        record = read_json_file(apply_path)
    record['apply_path'] = str(apply_path)
    return record


def latest(prefix: str) -> Optional[Path]:
    files = sorted(vault_dir().glob(prefix + '-*.json'))
    return files[-1] if files else None


def rollback(apply_path: Optional[Path] = None) -> Dict[str, Any]:
    apply_path = Path(apply_path) if apply_path else latest('apply')
    if not apply_path or not apply_path.exists():
        raise SystemExit('되돌릴 적용 기록이 없습니다')
    rec = read_json_file(apply_path)
    restored = []
    for b in rec['backups']:
        target = Path(b['target'])
        if b['backup_path']:
            shutil.copy2(b['backup_path'], target)
        elif target.exists():
            target.unlink()
        restored.append({'target': b['target'], 'ok': _sha(target) == b['sha256_before']})
    rec['rolled_back'] = True
    rec['rollback'] = {'at': now_iso(), 'restored': restored, 'ok': all(r['ok'] for r in restored)}
    _atomic_write(apply_path, json.dumps(rec, ensure_ascii=False, indent=1))
    return rec


def status() -> Dict[str, Any]:
    v = read_json_file(vault_json_path())
    last = latest('apply')
    rec = read_json_file(last) if last else None
    return {'vault_json': str(vault_json_path()), 'configured': isinstance(v, dict),
            'scopes': len((v or {}).get('scopes', {})) if isinstance(v, dict) else 0,
            'last_apply': str(last) if last else None,
            'last_apply_ok': (rec or {}).get('verify', {}).get('ok'), 'rolled_back': (rec or {}).get('rolled_back'),
            'last_analysis': str(latest('analysis') or '') or None, 'last_plan': str(latest('plan') or '') or None}


# ---------------------------------------------------------------- CLI

def _save(prefix: str, data: Dict[str, Any]) -> Path:
    p = vault_dir() / ('%s-%s.json' % (prefix, stamp()))
    _atomic_write(p, json.dumps(data, ensure_ascii=False, indent=1))
    return p


def main(argv: Optional[List[str]] = None) -> int:
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(prog='sb vault', description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('analyze')
    a.add_argument('--json', action='store_true')
    a.add_argument('--db')
    a.add_argument('--projects-root')
    a.add_argument('--no-git', action='store_true')
    p = sub.add_parser('plan')
    p.add_argument('--analysis')
    p.add_argument('--state-write', choices=('all', 'listed'), default='all',
                   help='all: 상태층 쓰기 허용을 바꾸지 않음(전 scope 허용, 기본) / listed: 최근 활동 scope 만 허용')
    p.add_argument('--json', action='store_true')
    x = sub.add_parser('apply')
    x.add_argument('--plan')
    x.add_argument('--yes', action='store_true', help='묻지 않고 적용(에이전트용 — --hash 필수)')
    x.add_argument('--hash', default='')
    r = sub.add_parser('rollback')
    r.add_argument('--apply')
    s = sub.add_parser('status')
    s.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)

    if args.cmd == 'analyze':
        res = analyze(args.db, args.projects_root, use_git=not args.no_git)
        path = _save('analysis', res)
        if args.json:
            print(json.dumps(dict(res, path=str(path)), ensure_ascii=False, indent=1))
        else:
            print('분석 저장: %s' % path)
            print('데이터 양: %s (관측 %d, 세션 %d, 작업 폴더 %d, 제외 폴더 %d)' % (
                res['data_volume'], res['sources'][0]['count'], res['sources'][0]['sessions'], res['work_folders'],
                res['excluded_count']))
            print('다음: sb vault plan')
        return 0
    if args.cmd == 'plan':
        src = Path(args.analysis) if args.analysis else latest('analysis')
        if not src:
            print('분석 결과가 없습니다 — sb vault analyze 먼저', file=sys.stderr)
            return 2
        plan = make_plan(read_json_file(src), str(src), args.state_write)
        path = _save('plan', plan)
        if args.json:
            print(json.dumps(dict(plan, path=str(path)), ensure_ascii=False, indent=1))
        else:
            print(render_plan(plan))
            print('\n저장: %s\n적용: sb vault apply --plan %s --yes --hash %s' % (path, path, plan['plan_hash'][:12]))
        return 0
    if args.cmd == 'apply':
        src = Path(args.plan) if args.plan else latest('plan')
        if not src:
            print('구성안이 없습니다 — sb vault plan 먼저', file=sys.stderr)
            return 2
        plan = read_json_file(src)
        approved = args.hash
        if not args.yes:
            print(render_plan(plan))
            try:
                ans = input('\n이 구성안을 적용할까요? 적용하려면 yes: ').strip().lower()
            except EOFError:
                ans = ''
            if ans != 'yes':
                print('적용하지 않았습니다')
                return 1
            approved = plan['plan_hash']
        rec = apply_plan(plan, approved, 'user_cli' if not args.yes else 'agent_user_utterance')
        ok = rec['verify']['ok']
        print(('적용 완료' if ok else '검증 실패 — 되돌렸습니다') + ': ' + rec['apply_path'])
        if ok:
            print('되돌리기: sb vault rollback --apply %s' % rec['apply_path'])
        return 0 if ok else 1
    if args.cmd == 'rollback':
        rec = rollback(Path(args.apply) if args.apply else None)
        print('되돌림 %s: %s' % ('완료' if rec['rollback']['ok'] else '일부 실패', ', '.join(
            '%s=%s' % (_short(r['target']), 'ok' if r['ok'] else 'FAIL') for r in rec['rollback']['restored'])))
        return 0 if rec['rollback']['ok'] else 1
    if args.cmd == 'status':
        st = status()
        print(json.dumps(st, ensure_ascii=False, indent=1) if args.json else
              '볼트: %s (scope %d개) / 마지막 적용: %s' % ('구성됨' if st['configured'] else '없음', st['scopes'],
                                                  st['last_apply'] or '없음'))
        return 0
    return 2


if __name__ == '__main__':
    sys.exit(main())
