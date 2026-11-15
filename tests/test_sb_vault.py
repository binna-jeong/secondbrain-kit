"""sb_vault: 기록 분석 → 구성안 → 적용·되돌리기. 격리 HOME·SB_HOME 에서만 돈다."""

import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / 'bin'
sys.path.insert(0, str(BIN))
import sb_vault  # noqa: E402


def sha(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


class VaultEnv(unittest.TestCase):
    """가짜 홈: 프로젝트 3개(git 1·폴더 2), 홈·임시 세션, 자동 메모리, claude-mem DB."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='sb-vault-test-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.home = self.root / 'home'
        self.sbhome = self.home / '.secondbrain'
        self.claude = self.home / '.claude'
        for d in (self.sbhome / 'config', self.claude / 'projects'):
            d.mkdir(parents=True)
        self.alpha = self.home / 'alpha'          # git 저장소, 하위 폴더 세션 포함
        (self.alpha / 'sub').mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(self.alpha)], check=True)
        self.beta = self.home / 'code' / 'beta'   # git 아님
        self.beta.mkdir(parents=True)
        self.small = self.home / 'small'          # 관측이 적어 scope 로 제안하지 않는다
        self.small.mkdir()
        env = mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'SB_HOME': str(self.sbhome), 'CLAUDE_CONFIG_DIR': str(self.claude),
            'SB_CLAUDE_MEM_DIR': str(self.home / '.claude-mem'), 'SB_VAULT_LLM': 'off',
            'SB_PROJECT_ALIASES': str(self.sbhome / 'config' / 'project_aliases.json')})
        env.start()
        self.addCleanup(env.stop)
        # 가짜 홈이 임시 폴더 아래라 그 슬러그가 '임시 폴더 세션'으로 걸러지지 않게 한다(실제 홈은 임시 폴더 밖)
        real_is_temp = sb_vault.sync_automemory.is_temp_slug
        home_slug = str(self.home).replace(os.sep, '-')
        tslug = mock.patch.object(sb_vault.sync_automemory, 'is_temp_slug',
                                  side_effect=lambda s: False if s.startswith(home_slug) else real_is_temp(s))
        tslug.start()
        self.addCleanup(tslug.stop)
        for k in ('SB_STATE_PILOT_SCOPES', 'SB_LOOPS_PATH', 'SB_VAULT_LLM_URL', 'SB_CORRECTION_LLM_URL'):
            os.environ.pop(k, None)
        self.db = self.home / '.claude-mem' / 'claude-mem.db'
        self.db.parent.mkdir()
        now = int(time.time() * 1000)
        old = now - 90 * 86400 * 1000
        con = sqlite3.connect(self.db)
        con.execute('CREATE TABLE sdk_sessions (memory_session_id TEXT, project TEXT, cwd TEXT, started_at_epoch INTEGER)')
        con.execute('CREATE TABLE observations (memory_session_id TEXT, project TEXT, type TEXT, created_at_epoch INTEGER)')
        sessions = [('a1', 'alpha', self.alpha, 60, now), ('a2', 'alpha', self.alpha / 'sub', 30, now),
                    ('b1', 'beta', self.beta, 40, old), ('h1', 'home', self.home, 500, now),
                    ('t1', 'trigger', Path(tempfile.gettempdir()) / 'x', 300, now), ('s1', 'small', self.small, 2, now)]
        for sid, tag, cwd, n, t in sessions:
            con.execute('INSERT INTO sdk_sessions VALUES (?,?,?,?)', (sid, tag, str(cwd), t))
            con.executemany('INSERT INTO observations VALUES (?,?,?,?)',
                            [(sid, tag, 'discovery' if i % 3 else 'decision', t) for i in range(n)])
        con.commit()
        con.close()
        # 자동 메모리: alpha 에 feedback 1개
        slug = self.claude / 'projects' / str(self.alpha).replace(os.sep, '-')
        (slug / 'memory').mkdir(parents=True)
        (slug / 'memory' / 'fb.md').write_text('---\nname: x\nmetadata:\n  type: feedback\n---\nbody\n', encoding='utf-8')
        (slug / 's.jsonl').write_text(json.dumps({'cwd': str(self.alpha)}) + '\n', encoding='utf-8')

    def analysis(self):
        return sb_vault.analyze(str(self.db), str(self.claude / 'projects'))


class AnalyzeTests(VaultEnv):
    def test_candidates_and_exclusions(self):
        a = self.analysis()
        keys = {p['suggested_key']: p for p in a['projects']}
        self.assertIn('alpha', keys)
        self.assertEqual(keys['alpha']['obs_count'], 90)          # 하위 폴더 세션이 git 뿌리로 묶인다
        self.assertIn('git', keys['alpha']['signals'])
        self.assertEqual(keys['alpha']['feedback'], 1)
        self.assertIn('beta', keys)
        self.assertNotIn('home', keys)
        classes = {e['class'] for e in a['excluded']}
        self.assertIn('home', classes)
        self.assertIn('temp', classes)
        self.assertEqual(a['data_volume'], 'thin')                 # 작업 폴더 2개 < 3
        self.assertFalse(a['llm']['used'])
        self.assertEqual(a['kinds']['discovery'] + a['kinds']['decision'], 932)

    def test_empty_volume_without_data(self):
        a = sb_vault.analyze(str(self.root / 'none.db'), str(self.root / 'none'))
        self.assertEqual(a['data_volume'], 'empty')
        self.assertFalse(a['sources'][0]['readable'])

    def test_no_network_without_llm(self):
        # LLM 미설정·off 이면 분석 중 소켓 연결이 한 번도 없어야 한다(개인 데이터 외부 전송 0)
        calls = []
        real = socket.socket.connect

        def guard(sock, addr):
            calls.append(addr)
            return real(sock, addr)
        with mock.patch.object(socket.socket, 'connect', guard):
            self.analysis()
            os.environ['SB_VAULT_LLM'] = 'auto'   # auto 여도 경로 설정이 없으면 부르지 않는다
            self.analysis()
        self.assertEqual(calls, [])

    def test_llm_receives_folder_parts_only(self):
        sent = []

        class Resp:
            def __init__(self, body): self.body = body
            def read(self): return self.body
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, timeout=0):
            sent.append(json.loads(req.data.decode()))
            return Resp(json.dumps({'choices': [{'message': {'content': '[{"i":0,"name":"renamed"}]'}}]}).encode())
        gen = self.home / 'src'
        gen.mkdir()
        con = sqlite3.connect(self.db)
        con.execute('INSERT INTO sdk_sessions VALUES (?,?,?,?)', ('g1', 'src', str(gen), int(time.time() * 1000)))
        con.executemany('INSERT INTO observations VALUES (?,?,?,?)', [('g1', 'src', 'discovery', 1)] * 30)
        con.commit(); con.close()
        with mock.patch.dict(os.environ, {'SB_VAULT_LLM': 'auto', 'SB_VAULT_LLM_URL': 'http://127.0.0.1:9/v1/chat/completions',
                                          'SB_VAULT_MODEL': 'm'}), \
                mock.patch.object(sb_vault.urllib.request, 'urlopen', fake):
            a = self.analysis()
        self.assertEqual(len(sent), 1)
        prompt = sent[0]['messages'][0]['content']
        self.assertNotIn(str(self.home), prompt)                   # 전체 경로·대화 내용은 보내지 않는다
        self.assertIn('renamed', {p['suggested_key'] for p in a['projects']})
        self.assertTrue(a['llm']['used'])


class PlanApplyTests(VaultEnv):
    def plan(self, mode='all'):
        return sb_vault.make_plan(self.analysis(), None, mode)

    def test_plan_filters_small_and_marks_activity(self):
        p = self.plan()
        keys = {s['key']: s for s in p['scopes']}
        self.assertNotIn('small', keys)
        self.assertIn('small', p['minor_folders'])
        self.assertTrue(keys['alpha']['state_write'])
        self.assertFalse(keys['beta']['state_write'])               # 90일 전 — 최근 활동 없음
        self.assertTrue(keys['alpha']['rules'])
        self.assertIsNone(p['state_env'])                           # all 모드는 env 를 바꾸지 않는다
        self.assertEqual(p['plan_hash'], sb_vault.plan_hash(p))
        self.assertIn('alpha', sb_vault.render_plan(p))

    def test_listed_mode_generates_env(self):
        p = self.plan('listed')
        self.assertEqual(p['state_env'], 'alpha')

    def test_existing_env_conflict_is_not_overwritten(self):
        settings = self.claude / 'settings.json'
        settings.write_text(json.dumps({'env': {'SB_STATE_PILOT_SCOPES': 'other'}}), encoding='utf-8')
        p = self.plan('listed')
        self.assertIsNone(p['state_env'])
        self.assertTrue(any(c['key'] == 'SB_STATE_PILOT_SCOPES' for c in p['conflicts']))
        before = sha(settings)
        rec = sb_vault.apply_plan(p, p['plan_hash'])
        self.assertTrue(rec['verify']['ok'])
        self.assertEqual(sha(settings), before)

    def test_existing_alias_conflict_kept(self):
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text(json.dumps({'beta': [str(self.home / 'elsewhere')]}), encoding='utf-8')
        p = self.plan()
        self.assertTrue(any(c['key'] == 'beta' for c in p['conflicts']))
        self.assertNotIn('beta', p['aliases'])
        rec = sb_vault.apply_plan(p, p['plan_hash'])
        self.assertTrue(rec['verify']['ok'])
        self.assertEqual(json.loads(ap.read_text())['beta'], [str(self.home / 'elsewhere')])

    def test_apply_then_rollback_restores_bytes(self):
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text('{"keep": ["/x"]}\n', encoding='utf-8')
        settings = self.claude / 'settings.json'
        settings.write_text('{"env": {"A": "1"}}\n', encoding='utf-8')
        targets = [ap, sb_vault.vault_json_path(), sb_vault.loops_path(), settings]
        before = {str(t): sha(t) for t in targets}
        p = self.plan('listed')
        rec = sb_vault.apply_plan(p, p['plan_hash'][:12])
        self.assertTrue(rec['verify']['ok'], rec['verify'])
        self.assertEqual(json.loads(settings.read_text())['env']['SB_STATE_PILOT_SCOPES'], 'alpha')
        self.assertEqual(json.loads(settings.read_text())['env']['A'], '1')
        self.assertIn('keep', json.loads(ap.read_text()))
        self.assertTrue(sb_vault.vault_json_path().exists())
        rb = sb_vault.rollback(Path(rec['apply_path']))
        self.assertTrue(rb['rollback']['ok'])
        self.assertEqual({str(t): sha(t) for t in targets}, before)

    def test_symlinked_settings_stays_a_link(self):
        real = self.root / 'dotfiles' / 'settings.json'
        real.parent.mkdir()
        real.write_text('{"env": {}}\n', encoding='utf-8')
        link = self.claude / 'settings.json'
        link.symlink_to(real)
        p = self.plan('listed')
        rec = sb_vault.apply_plan(p, p['plan_hash'])
        self.assertTrue(rec['verify']['ok'])
        self.assertTrue(link.is_symlink())
        self.assertEqual(json.loads(real.read_text())['env']['SB_STATE_PILOT_SCOPES'], 'alpha')
        sb_vault.rollback(Path(rec['apply_path']))
        self.assertTrue(link.is_symlink())
        self.assertEqual(real.read_text(), '{"env": {}}\n')

    def test_rollback_refuses_after_user_edit(self):
        p = self.plan()
        rec = sb_vault.apply_plan(p, p['plan_hash'])
        ap = Path(p['aliases_path'])
        ap.write_text('{"mine": ["/y"]}\n', encoding='utf-8')   # 적용 뒤 사용자가 고침
        with self.assertRaises(SystemExit):
            sb_vault.rollback(Path(rec['apply_path']))
        self.assertIn('mine', ap.read_text())
        sb_vault.rollback(Path(rec['apply_path']), force=True)
        with self.assertRaises(SystemExit):                       # 두 번 되돌리지 않는다
            sb_vault.rollback(Path(rec['apply_path']))

    def test_failed_verify_rolls_back_automatically(self):
        ap = self.sbhome / 'config' / 'project_aliases.json'
        ap.write_text('{}\n', encoding='utf-8')
        before = sha(ap)
        p = self.plan()
        rec = sb_vault.apply_plan(p, p['plan_hash'], _fail_after_write=True)
        self.assertFalse(rec['verify']['ok'])
        self.assertTrue(rec['rolled_back'])
        self.assertEqual(sha(ap), before)
        self.assertFalse(sb_vault.vault_json_path().exists())

    def test_hash_guard(self):
        p = self.plan()
        with self.assertRaises(SystemExit):
            sb_vault.apply_plan(p, 'deadbeef')
        p['scopes'].append({'key': 'sneaky'})
        with self.assertRaises(SystemExit):
            sb_vault.apply_plan(p, p['plan_hash'])

    def test_vault_json_drives_state_env(self):
        p = self.plan('listed')
        sb_vault.apply_plan(p, p['plan_hash'])
        v = json.loads(sb_vault.vault_json_path().read_text())
        listed = sorted(k for k, s in v['scopes'].items() if s['state_write'])
        env = json.loads((self.claude / 'settings.json').read_text())['env']['SB_STATE_PILOT_SCOPES']
        self.assertEqual(env, ','.join(listed))

    def test_cli_roundtrip(self):
        self.assertEqual(sb_vault.main(['status']), 0)
        with mock.patch.object(sb_vault, 'analyze', return_value=self.analysis()):
            self.assertEqual(sb_vault.main(['analyze']), 0)
        self.assertEqual(sb_vault.main(['plan']), 0)
        plan = json.loads(sb_vault.latest('plan').read_text())
        self.assertEqual(sb_vault.main(['apply', '--yes', '--hash', plan['plan_hash'][:12]]), 0)
        self.assertTrue(sb_vault.status()['configured'])
        self.assertEqual(sb_vault.main(['rollback']), 0)
        self.assertFalse(sb_vault.status()['configured'])
        with mock.patch('builtins.input', return_value='no'):
            self.assertEqual(sb_vault.main(['apply']), 1)


class OnboardTests(VaultEnv):
    def test_init_default_is_idempotent(self):
        r = sb_vault.init_default()
        self.assertEqual(len(r['created']), 2)
        v = json.loads(sb_vault.vault_json_path().read_text())
        self.assertTrue(v['default'])
        self.assertEqual(v['kind_policy'], {'default': 'normal'})
        self.assertIn('~', v['scope_excluded'])
        before = sha(sb_vault.vault_json_path())
        self.assertEqual(sb_vault.init_default()['created'], [])
        self.assertEqual(sha(sb_vault.vault_json_path()), before)

    def _add_ids(self, n):
        con = sqlite3.connect(self.db)
        cols = [r[1] for r in con.execute('PRAGMA table_info(observations)')]
        if 'id' not in cols:   # 실제 claude-mem 표처럼 id 열을 둔다
            con.execute('ALTER TABLE observations ADD COLUMN id INTEGER')
            con.execute('UPDATE observations SET id = rowid')
        con.execute('UPDATE observations SET id = rowid + ?', (n,))
        con.commit(); con.close()

    def test_nudge_default_vault_with_enough_records(self):
        sb_vault.init_default()
        self._add_ids(0)          # 관측 932건 ≥ 500
        line = sb_vault.nudge_line('2026-11-15', str(self.db))
        self.assertIn('sb vault analyze', line)
        self.assertEqual(sb_vault.nudge_line('2026-11-15', str(self.db)), '')   # 하루 1회
        self.assertIn('sb vault', sb_vault.nudge_line('2026-11-16', str(self.db)))
        with mock.patch.dict(os.environ, {'SB_VAULT_NUDGE': '0'}):
            self.assertEqual(sb_vault.nudge_line('2026-11-17', str(self.db)), '')

    def test_nudge_quiet_after_configuring_until_growth(self):
        self._add_ids(0)
        p = sb_vault.make_plan(self.analysis(), None, 'all')
        sb_vault.apply_plan(p, p['plan_hash'])
        self.assertEqual(json.loads(sb_vault.vault_json_path().read_text())['obs_seen'], 932)
        self.assertEqual(sb_vault.nudge_line('2026-11-15', str(self.db)), '')   # 막 구성했다
        self._add_ids(5000)       # max id 5932 ≥ 932*1.5 그리고 +2000 이상
        self.assertIn('늘었다', sb_vault.nudge_line('2026-11-16', str(self.db)))

    def test_nudge_silent_without_db(self):
        self.assertEqual(sb_vault.nudge_line('2026-11-15', str(self.root / 'none.db')), '')


class InstallerOnboardTests(VaultEnv):
    def _inst(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('kit_install_vault', BIN.parent / 'installer' / 'install.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _args(self, **kw):
        import argparse
        return argparse.Namespace(**dict({'no_vault_llm': True}, **kw))

    def test_thin_data_makes_default_vault_only(self):
        inst = self._inst()
        calls = []

        def fake_run(cmd, check=True, capture=False, env=None, cwd=None):
            calls.append(cmd[2])
            out = json.dumps({'data_volume': 'thin'}) if cmd[2] == 'analyze' else ''
            return subprocess.CompletedProcess(cmd, 0, out, '')
        with mock.patch.object(inst, 'run', fake_run), mock.patch.object(inst, 'say'):
            inst.vault_onboarding(self._args())
        self.assertEqual(calls, ['init', 'analyze'])          # 기록이 적으면 구성안을 만들지 않는다
        self.assertNotIn('apply', calls)

    def test_enough_data_makes_plan_but_never_applies(self):
        inst = self._inst()
        calls, envs = [], []

        def fake_run(cmd, check=True, capture=False, env=None, cwd=None):
            calls.append(cmd[2]); envs.append(env or {})
            out = json.dumps({'data_volume': 'enough'}) if cmd[2] == 'analyze' else '구성안'
            return subprocess.CompletedProcess(cmd, 0, out, '')
        with mock.patch.object(inst, 'run', fake_run), mock.patch.object(inst, 'say') as say:
            inst.vault_onboarding(self._args())
        self.assertEqual(calls, ['init', 'analyze', 'plan'])
        self.assertEqual(envs[1].get('SB_VAULT_LLM'), 'off')
        self.assertTrue(any('sb vault apply' in str(c) for c in say.call_args_list))

    def test_analyze_failure_is_warning(self):
        inst = self._inst()
        with mock.patch.object(inst, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'boom')), \
                mock.patch.object(inst, 'say'), mock.patch.object(inst, 'WARNINGS', []) as w:
            inst.vault_onboarding(self._args())
        self.assertEqual(len(w), 1)

    def test_real_end_to_end_on_isolated_home(self):
        # 실제 sb_vault.py 를 자식 프로세스로: 빈 SB_HOME → init·analyze(→ thin) → 기본 볼트
        inst = self._inst()
        inst.SB_HOME = self.sbhome
        inst.VPY = Path(sys.executable)
        with mock.patch.object(inst, 'say'):
            inst.vault_onboarding(self._args())
        v = json.loads(sb_vault.vault_json_path().read_text())
        self.assertTrue(v['default'])
        self.assertTrue(list(sb_vault.vault_dir().glob('analysis-*.json')))


if __name__ == '__main__':
    unittest.main()
