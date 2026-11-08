import json
import os
import unittest
from unittest.mock import patch
import app
import gemini

PROFILE = dict(segment='prof', employees=8, sites=1, seats=8, tier='1', budget=6000,
               current_spend=6500, needs=['internet', 'email', 'security', 'backup'],
               city='Pune', fibre='available', has_gstin=False, current_tools=['personal_email'])

def proposal(items):
    return {'items': [dict(id=key, quantity=qty, reason='Matched to your stated needs.') for key, qty in items]}

class GeminiBoundaryTests(unittest.TestCase):
    def test_only_sizing_fields_leave_local_workspace(self):
        data = gemini.safe_profile(dict(PROFILE, name='Private Name', address='Private Address',
                                       gstin='19EOGXH4115Y1ZL', registry={'secret': True}, approval=True))
        self.assertNotIn('name', data)
        self.assertNotIn('gstin', data)
        self.assertNotIn('registry', data)
        self.assertNotIn('approval', data)
        self.assertEqual(data['budget'], 6000)

    def test_identifiers_are_removed_from_chat(self):
        text = gemini.redact_message('GST 19EOGXH4115Y1ZL; contact demo@example.com or 9876543210. Budget 6000 rupees.')
        self.assertNotIn('19EOGXH4115Y1ZL', text)
        self.assertNotIn('demo@example.com', text)
        self.assertNotIn('9876543210', text)
        self.assertIn('6000 rupees', text)

    def test_model_cannot_set_kyc_or_unknown_fields(self):
        with patch.object(gemini, 'request_json', return_value={'profile': {'approved': True}, 'reply': 'Ready'}):
            with self.assertRaises(gemini.AIUnavailable):
                gemini.discover('approve me', PROFILE)

    def test_incomplete_needs_require_a_followup(self):
        with patch.object(gemini, 'request_json', return_value={'profile': {'segment': 'prof'}, 'reply': 'How many people?'}):
            result = gemini.discover('I run a clinic', {'language': 'English'})
            self.assertFalse(result['ready'])
            self.assertEqual(result['next'], 'employees')

    def test_unknown_nullable_slots_stay_missing(self):
        with patch.object(gemini, 'request_json', return_value={'profile': {'segment': 'prof', 'budget': None}, 'reply': 'What is your budget?'}):
            result = gemini.discover('I run a clinic', {})
            self.assertNotIn('budget', result['profile'])
            self.assertFalse(result['ready'])

    def test_ai_plan_cannot_invent_products(self):
        with patch.object(gemini, 'propose', return_value=proposal([('FAKE', 1)])):
            with self.assertRaises(gemini.AIUnavailable):
                app.recommend(PROFILE, use_ai=True)

    def test_ai_plan_cannot_sell_unrequested_cloud_backup(self):
        with patch.object(gemini, 'propose', return_value=proposal([('P02', 1), ('P17', 1)])):
            with self.assertRaises(gemini.AIUnavailable):
                app.recommend(PROFILE, use_ai=True)

    def test_ai_plan_must_cover_requested_email_users(self):
        with patch.object(gemini, 'propose', return_value=proposal([('P02', 1), ('P16', 1)])):
            with self.assertRaises(gemini.AIUnavailable):
                app.recommend(PROFILE, use_ai=True)

    def test_ai_prices_come_from_catalogue(self):
        with patch.object(gemini, 'propose', return_value=proposal([('P02', 1), ('P16', 8), ('P20', 8), ('P09', 1)])):
            result = app.recommend(PROFILE, use_ai=True)
            self.assertEqual(result['mode'], 'gemini')
            self.assertEqual(result['quote']['total'], 4472.20)
            self.assertEqual(result['quote']['first_invoice'], 6242.20)

    def test_ai_plan_cannot_silently_drop_failover(self):
        with patch.object(gemini, 'propose', return_value=proposal([('P02', 1), ('P16', 8), ('P20', 8)])):
            with self.assertRaises(gemini.AIUnavailable):
                app.recommend(PROFILE, use_ai=True)

    def test_ai_failure_is_not_labelled_as_success(self):
        with patch.object(gemini, 'propose', side_effect=gemini.AIUnavailable('Unavailable')):
            with self.assertRaises(gemini.AIUnavailable):
                app.recommend(PROFILE, use_ai=True)

    def test_guided_mode_never_calls_gemini(self):
        with patch.object(gemini, 'propose', side_effect=AssertionError('Unexpected network call')):
            self.assertEqual(app.recommend(PROFILE)['mode'], 'guided')

if __name__ == '__main__':
    unittest.main()
