from pathlib import Path
import importlib
import pkgutil
import unittest

import kefu
from kefu.plugins.knowledge import InMemoryKnowledgeRepository, KnowledgePlugin

ROOT = Path(__file__).resolve().parents[1]


class Imports(unittest.TestCase):
    def test_core_imports_and_resources(self):
        for name in ['kefu.app', 'kefu.cli', 'kefu.worker', 'kefu.runtime.loop', 'kefu.services']:
            importlib.import_module(name)
        for path in ['web/src/App.tsx', 'migrations/env.py', 'deploy/Dockerfile',
                     'skills/guided-troubleshooting/SKILL.md', 'data/fixtures/s1-pro-e73.png']:
            self.assertTrue((ROOT / path).is_file(), path)


class Knowledge(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.plugin = KnowledgePlugin(InMemoryKnowledgeRepository())
        for name in ['sources.jsonl', 'official_expansion_2026-10-08.jsonl',
                     'official_products_2026-10-08.jsonl']:
            await self.plugin.import_manifest(ROOT / 'data' / name)

    async def test_ambiguous_product_requires_confirmation(self):
        result = await self.plugin.resolve_product('S1 Pro 不吸了')
        ids = {p['id'] for p in result['data']['candidates']}
        self.assertTrue({'eufy-t2080', 'eufy-t8d04'} <= ids)
        self.assertTrue(result['data']['confirmation_required'])

    async def test_search_stays_in_confirmed_product(self):
        result = await self.plugin.search('不吸了', product='eufy-t2080')
        self.assertEqual(result['status'], 'ok')
        evidence = result['data']['evidence']
        self.assertTrue(evidence)
        self.assertTrue(all('eufy-t2080' in e['product_ids'] for e in evidence))

    async def test_unsupported_question_returns_no_evidence(self):
        result = await self.plugin.search('吸奶器的滚刷堵了怎么清', product='eufy-t8d04')
        self.assertEqual(result['status'], 'not_found')
        self.assertEqual(result['data']['evidence'], [])


if __name__ == '__main__':
    unittest.main()
