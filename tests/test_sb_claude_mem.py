"""sb_claude_mem: claude-mem 버전·chroma-mcp 워커 인자 기준값과 worker-service.cjs 파서."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / 'bin'
FIX = Path(__file__).resolve().parent / 'fixtures' / 'claude-mem'
sys.path.insert(0, str(BIN))
import sb_claude_mem as cm  # noqa: E402

# 실제 npm 패키지의 worker-service.cjs 에서 잘라 낸 조각(정의 2개 + buildLauncherPrefix)
EXPECTED = {
    '13.24.23': ['onnxruntime>=1.20', 'protobuf<7'],
    '13.34.2': ['onnxruntime>=1.20', 'protobuf<7', 'chromadb==1.5.9'],
    '13.39.0': ['onnxruntime>=1.20', 'protobuf<7', 'chromadb==1.5.9', 'pydantic<2.14'],
}


def fixture(version: str) -> str:
    return (FIX / (version + '.cjs')).read_text(encoding='utf-8')


class ParseTests(unittest.TestCase):
    def test_real_fixtures(self):
        for version, withs in EXPECTED.items():
            got, ver, ok = cm.parse_worker_spec(fixture(version))
            self.assertTrue(ok, version)
            self.assertEqual(got, withs, version)
            self.assertEqual(ver, '0.2.6', version)

    def test_inline_literals(self):
        # 압축기가 상수를 인라인하면 정의 없이 리터럴이 들어온다
        text = ('class A{static buildLauncherPrefix(e){return["--python",e,"--with","onnxruntime>=1.20",'
                '"--with","chromadb==1.6.0","--from","chroma-mcp==0.3.1","chroma-mcp"]}static b(){}}')
        withs, ver, ok = cm.parse_worker_spec(text)
        self.assertTrue(ok)
        self.assertEqual(withs, ['onnxruntime>=1.20', 'chromadb==1.6.0'])
        self.assertEqual(ver, '0.3.1')

    def test_broken_falls_back(self):
        for text in ('', 'class A{static other(e){}}',
                     'static buildLauncherPrefix(e){let r=NOPE.flatMap(n=>["--with",n]);return["--from",`chroma-mcp==${ALSO}`]}'):
            withs, ver, ok = cm.parse_worker_spec(text)
            self.assertFalse(ok)
            self.assertEqual(withs, cm.FALLBACK_WITH)
            self.assertEqual(ver, cm.FALLBACK_CHROMA_MCP)

    def test_member_access_is_not_a_definition(self):
        # obj.BE=[...] 는 정의가 아니다 — 앞에 ., 영숫자, $ 가 붙은 이름은 건너뛴다
        text = ('x.BE=["evil"];var BE=["onnxruntime>=1.20"],F0="0.2.6";'
                'class G{static buildLauncherPrefix(e){let r=BE.flatMap(n=>["--with",n]);'
                'return["--python",e,...r,"--from",`chroma-mcp==${F0}`,"chroma-mcp"]}static z(){}}')
        withs, ver, ok = cm.parse_worker_spec(text)
        self.assertTrue(ok)
        self.assertEqual(withs, ['onnxruntime>=1.20'])


class RequirementTests(unittest.TestCase):
    def test_tool_requirements_adds_pydantic_once(self):
        for version, withs in EXPECTED.items():
            reqs = cm.tool_requirements(withs)
            self.assertTrue(set(withs) <= set(reqs), version)
            self.assertEqual(sum(1 for r in reqs if r.startswith('pydantic')), 1, version)

    def test_worker_args_shape(self):
        self.assertEqual(cm.worker_args(['a', 'b'], '0.2.6'),
                         ['--with', 'a', '--with', 'b', '--from', 'chroma-mcp==0.2.6', 'chroma-mcp'])

    def test_pinned_chromadb(self):
        self.assertEqual(cm.pinned_chromadb(EXPECTED['13.34.2']), '1.5.9')
        self.assertIsNone(cm.pinned_chromadb(EXPECTED['13.24.23']))

    def test_python_version_order(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('CLAUDE_MEM_PYTHON_VERSION', None)
            self.assertEqual(cm.python_version({}), '3.13')
            self.assertEqual(cm.python_version({'CLAUDE_MEM_PYTHON_VERSION': '3.12'}), '3.12')
            with mock.patch.dict(os.environ, {'CLAUDE_MEM_PYTHON_VERSION': '3.11'}):
                self.assertEqual(cm.python_version({'CLAUDE_MEM_PYTHON_VERSION': '3.12'}), '3.11')


class VersionTests(unittest.TestCase):
    def test_vkey_and_range(self):
        self.assertEqual(cm.vkey('13.34.2'), (13, 34, 2))
        self.assertEqual(cm.vkey('v13.35.0-beta.1'), (13, 35, 0))
        self.assertEqual(cm.vkey('abc'), ())
        self.assertTrue(cm.in_tested(cm.RECOMMENDED))
        self.assertTrue(cm.in_tested(cm.TESTED[0]))
        self.assertFalse(cm.in_tested('13.24.5'))
        self.assertFalse(cm.in_tested('15.18.3'))
        self.assertFalse(cm.in_tested(None))

    def test_recommended_matches_fallback(self):
        # 기본값은 권장판의 워커 인자여야 한다
        self.assertEqual(cm.FALLBACK_WITH, EXPECTED[cm.RECOMMENDED])

    def test_plugin_root_picks_highest_non_orphan(self):
        tmp = Path(tempfile.mkdtemp())
        base = tmp / 'plugins' / 'cache' / 'thedotmack' / 'claude-mem'
        for v, orphan in (('13.24.23', False), ('13.34.2', False), ('13.40.0', True), ('junk', False)):
            (base / v / 'scripts').mkdir(parents=True)
            (base / v / 'scripts' / 'worker-service.cjs').write_text(fixture('13.34.2'), encoding='utf-8')
            if orphan:
                (base / v / '.orphaned_at').write_text('x', encoding='utf-8')
        (base / '13.99.0').mkdir()   # scripts 없음 — 고아 판정과 같은 취급
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': str(tmp)}):
            root = cm.plugin_root()
            self.assertEqual(root.name, '13.34.2')
            self.assertEqual(cm.installed_version(), '13.34.2')
            self.assertEqual(cm.worker_chroma_spec()[0], EXPECTED['13.34.2'])
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': tempfile.mkdtemp()}):
            self.assertIsNone(cm.plugin_root())
            self.assertFalse(cm.worker_chroma_spec()[2])


if __name__ == '__main__':
    unittest.main()
