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


if __name__ == '__main__':
    unittest.main()
