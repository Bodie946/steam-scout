import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import scout


class ScoutTests(unittest.TestCase):
    def setUp(self):
        self.library, self.deals = scout.demo_data()
        self.cfg = {'max_price': 30, 'min_discount': 20, 'top_n': 20, 'favorite_tags': [],
                    'country': 'US', 'steam_id': '12345678901234567', 'library_sample': 1, 'max_deals': 2}

    def test_owned_excluded_even_outside_sample(self):
        result = scout.rank(self.library, self.deals, self.cfg, {2, 3})
        self.assertEqual([g['appid'] for g in result], [4])

    def test_played_tags_rank_matching_game_first(self):
        result = scout.rank(self.library, self.deals, self.cfg)
        self.assertEqual(result[0]['appid'], 2)
        self.assertIn('strategy', result[0]['reason'])

    def test_budget_tags_and_discount(self):
        self.cfg.update(max_price=15, excluded_tags=['SIMULATION'])
        self.assertEqual([g['appid'] for g in scout.rank(self.library, self.deals, self.cfg)], [2])
        self.cfg['min_discount'] = 60
        self.assertEqual(scout.rank(self.library, self.deals, self.cfg), [])

    def test_only_current_steam_base_games_with_ids(self):
        bad = []
        for changes in [{'type': 'dlc'}, {'appid': None}, {'deal': dict(self.deals[0]['deal'], shop={'name': 'Elsewhere'})},
                        {'deal': dict(self.deals[0]['deal'], expiry=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat())}]:
            bad.append(dict(copy.deepcopy(self.deals[0]), **changes))
        self.assertEqual(scout.rank(self.library, bad, self.cfg), [])

    def test_unplayed_not_taste_and_manual_preference(self):
        self.library[0]['playtime_forever'] = 0
        self.assertEqual(scout.taste(self.library, []), {})
        self.assertEqual(scout.taste(self.library, ['RPG']), {'rpg': 1})

    def test_html_escapes_provider_text_and_rejects_script_url(self):
        self.deals[0]['title'] = '<script>alert(1)</script>'
        self.deals[0]['deal']['url'] = 'javascript:alert(1)'
        with tempfile.TemporaryDirectory() as folder, patch.object(scout, 'ROOT', Path(folder)):
            path = scout.report(scout.rank(self.library, self.deals, self.cfg), self.cfg, True, 3)
            result = path.read_text(encoding='utf-8')
        self.assertNotIn('<script>', result)
        self.assertNotIn('javascript:', result)
        self.assertIn('FICTIONAL', result)

    def test_private_library_fails_clearly(self):
        class Fake:
            def get(self, *args, **kwargs):
                return {'response': {}}
        with self.assertRaisesRegex(scout.APIError, 'Game details public'):
            scout.collect(Fake(), self.cfg, {'steam_api_key': 'test'})

    def test_paginated_deals_and_full_owned_set(self):
        parent = self
        class Fake:
            def get(self, *args, **kwargs):
                return {'response': {'games': parent.library + [{'appid': 99, 'playtime_forever': 0}]}}
            def itad(self, path, **kw):
                if 'lookup' in path:
                    return {'found': False}
                if 'shops' in path:
                    return [{'title': 'Steam', 'id': 61}]
                if 'deals' in path:
                    game = dict(parent.deals[kw['offset']], id=str(kw['offset']))
                    return {'list': [game], 'hasMore': kw['offset'] == 0, 'nextOffset': 1}
                return parent.deals[int(kw['id'])]
        library, deals, owned, scanned = scout.collect(Fake(), self.cfg, {'steam_api_key': 'test'})
        self.assertEqual(scanned, 2)
        self.assertEqual(len(deals), 2)
        self.assertIn(99, owned)
        self.assertEqual(len(library), 1)


if __name__ == '__main__':
    unittest.main()
