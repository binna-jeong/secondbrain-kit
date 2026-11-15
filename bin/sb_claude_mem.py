#!/usr/bin/env python3
"""claude-mem 버전과 chroma-mcp 실행 인자의 기준값을 한 곳에 둔다(설치기·doctor·sb health 가 같이 쓴다).

claude-mem 워커는 벡터 동기화용 chroma-mcp 를 자기 인자로 띄운다(worker-service.cjs 의 buildLauncherPrefix):
  uvx --python <py> --with <요구 1> --with <요구 2> ... --from chroma-mcp==<버전> chroma-mcp
이 인자는 claude-mem 버전마다 다르다(2026-11-15, npm 18개 버전 비교):
  13.24.x~13.28.x  onnxruntime>=1.20, protobuf<7
  13.29.0~13.34.x  + chromadb==1.5.9
  13.35.0 이상     + pydantic<2.14 (본가가 직접 고정)
키트는 이 값을 복사해 두지 않고, 설치된 플러그인의 worker-service.cjs 에서 읽는다. 읽지 못하면 아래 기본값을 쓰고
ok=False 를 돌려준다 — 경고는 부르는 쪽이 낸다.

pydantic<2.14 는 일부러 고정하는 값이다: chroma-mcp 0.2.6 이 고정한 mcp 1.6.0 이 pydantic 2.14 에서 사라진
_typing_extra.eval_type_backport 를 import 해 시작 즉시 죽는다(2026-10-12 벡터 동기화 4시간+ 정지).

  python bin/sb_claude_mem.py          → 설치판·검증 범위·워커 인자 요약(JSON)
"""
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# 설치기가 새로 깔 때 쓰는 권장판(오너 맥 실가동판). 바꾸면 TESTED·tests/fixtures/claude-mem 도 함께 본다.
RECOMMENDED = '13.34.2'
# 키트와 함께 돌려 본 범위(양끝 포함) — 13.24.23 은 예전 설치기 기본판, 13.34.2 는 오너 맥 실가동판.
# 밖이면 doctor·sb health 가 경고만 한다(차단 없음). 워커 인자 파서 자체는 13.24.23~15.18.3 의 npm 패키지 21개에서
# 같은 구조로 읽혔다(2026-11-15) — 그래도 키트의 DB 읽기·재시작 경로는 이 범위에서만 확인했다.
TESTED = ('13.24.23', '13.34.2')
PYDANTIC_PIN = 'pydantic<2.14'
# 파싱 실패 시 기본값 = 권장판의 워커 인자
FALLBACK_WITH = ['onnxruntime>=1.20', 'protobuf<7', 'chromadb==1.5.9']
FALLBACK_CHROMA_MCP = '0.2.6'
DEFAULT_PYTHON = '3.13'


def vkey(v: str) -> Tuple[int, ...]:
    """'13.34.2' → (13, 34, 2). 숫자가 아닌 꼬리(-beta 등)는 버린다. 못 읽으면 ()."""
    try:
        return tuple(int(x) for x in re.split(r'[-+]', str(v).strip().lstrip('v'))[0].split('.'))
    except ValueError:
        return ()


def in_tested(v: Optional[str]) -> bool:
    k = vkey(v or '')
    return bool(k) and vkey(TESTED[0]) <= k <= vkey(TESTED[1])


def plugin_base() -> Path:
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or '~/.claude').expanduser() / 'plugins' / 'cache' / 'thedotmack' / 'claude-mem'


def plugin_root() -> Optional[Path]:
    """설치된 claude-mem 플러그인 중 가장 높은 버전(고아 표시·scripts 없는 폴더 제외)."""
    base = plugin_base()
    best = None
    for d in base.glob('*') if base.is_dir() else []:
        if (d / '.orphaned_at').exists() or not (d / 'scripts' / 'worker-service.cjs').is_file():
            continue
        key = vkey(d.name)
        if not key:
            continue
        if best is None or key > best[0]:
            best = (key, d)
    return best[1] if best else None


def installed_version(root: Optional[Path] = None) -> Optional[str]:
    root = root or plugin_root()
    return root.name if root else None


_PREFIX = re.compile(r'buildLauncherPrefix\(\w+\)\{(.{0,600}?)\}(?=static\b|\}|$)', re.S)


def _string_def(text: str, name: str) -> Optional[str]:
    m = re.search(r'(?<![\w$.])' + re.escape(name) + r'="([^"\\]*)"', text)
    return m.group(1) if m else None


def _array_def(text: str, name: str) -> Optional[List[str]]:
    m = re.search(r'(?<![\w$.])' + re.escape(name) + r'=\[((?:"[^"\\]*",?)*)\]', text)
    return re.findall(r'"([^"\\]*)"', m.group(1)) if m else None


def parse_worker_spec(text: str) -> Tuple[List[str], str, bool]:
    """worker-service.cjs 본문에서 (워커 --with 목록, chroma-mcp 버전, 읽기 성공)."""
    m = _PREFIX.search(text or '')
    if not m:
        return list(FALLBACK_WITH), FALLBACK_CHROMA_MCP, False
    body = m.group(1)
    ver = None
    vm = re.search(r'chroma-mcp==\$\{([\w$]+)\}', body)
    if vm:
        ver = _string_def(text, vm.group(1))
    else:
        lm = re.search(r'chroma-mcp==([0-9][\w.]*)', body)
        ver = lm.group(1) if lm else None
    withs = None
    fm = re.search(r'([\w$]+)\.flatMap\(', body)
    if fm:
        withs = _array_def(text, fm.group(1))
    else:
        found = re.findall(r'"--with","([^"\\]*)"', body)
        withs = found if found else None
    if not ver or withs is None or not vkey(ver):
        return list(FALLBACK_WITH), FALLBACK_CHROMA_MCP, False
    return withs, ver, True


def worker_chroma_spec(root: Optional[Path] = None) -> Tuple[List[str], str, bool]:
    """설치된 워커의 (--with 목록, chroma-mcp 버전, 읽기 성공). 플러그인이 없거나 못 읽으면 기본값과 False."""
    root = root or plugin_root()
    if not root:
        return list(FALLBACK_WITH), FALLBACK_CHROMA_MCP, False
    try:
        text = (root / 'scripts' / 'worker-service.cjs').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return list(FALLBACK_WITH), FALLBACK_CHROMA_MCP, False
    return parse_worker_spec(text)


def python_version(settings: Optional[dict] = None) -> str:
    """워커가 쓰는 파이썬 버전 — 워커와 같은 규칙(환경변수 > claude-mem 설정 > 3.13)."""
    s = settings or {}
    return os.environ.get('CLAUDE_MEM_PYTHON_VERSION') or s.get('CLAUDE_MEM_PYTHON_VERSION') or DEFAULT_PYTHON


def worker_args(withs: List[str], chroma_ver: str) -> List[str]:
    """워커와 같은 uvx 인자(--python 은 부르는 쪽이 앞에 붙인다)."""
    out: List[str] = []
    for w in withs:
        out += ['--with', w]
    return out + ['--from', 'chroma-mcp==%s' % chroma_ver, 'chroma-mcp']


def tool_requirements(withs: List[str]) -> List[str]:
    """고정 도구에 넣을 요구 = 워커 요구 전부 + pydantic 고정(워커가 이미 pydantic 을 고정하면 더하지 않는다).
    uvx 는 이름이 같은 도구가 워커의 모든 --with 를 이미 만족할 때만 그 도구를 재사용한다(2026-11-15 격리 실측).
    하나라도 빠지면 경고 없이 새 환경을 풀고 pydantic 최신판을 받는다."""
    reqs = list(withs)
    if not any(re.match(r'pydantic\b', w) for w in reqs):
        reqs.append(PYDANTIC_PIN)
    return reqs


def pinned_chromadb(withs: List[str]) -> Optional[str]:
    for w in withs:
        m = re.match(r'chromadb==([\w.]+)$', w)
        if m:
            return m.group(1)
    return None


def summary() -> dict:
    root = plugin_root()
    withs, ver, ok = worker_chroma_spec(root)
    v = installed_version(root)
    return {'installed': v, 'recommended': RECOMMENDED, 'tested': list(TESTED), 'in_tested': in_tested(v),
            'worker_with': withs, 'chroma_mcp': ver, 'parsed': ok, 'tool_requirements': tool_requirements(withs)}


if __name__ == '__main__':
    print(json.dumps(summary(), ensure_ascii=False, indent=1))
    sys.exit(0)
