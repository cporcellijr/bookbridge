"""HardcoverClient.get_user_book distinguishes 'no UserBook' ({}) from a failed lookup (None)."""
import unittest
from unittest.mock import patch

from src.api.hardcover_client import HardcoverClient, HardcoverRateLimitError


class TestGetUserBookResultKinds(unittest.TestCase):
    def setUp(self) -> None:
        self.client = HardcoverClient.__new__(HardcoverClient)
        self.client.user_id = 7

    def test_confirmed_empty_returns_empty_dict(self) -> None:
        with patch.object(self.client, 'query', return_value={'user_books': []}):
            self.assertEqual(self.client.get_user_book('42'), {})

    def test_found_returns_entry(self) -> None:
        with patch.object(self.client, 'query', return_value={'user_books': [{'id': 1, 'status_id': 2}]}):
            self.assertEqual(self.client.get_user_book('42'), {'id': 1, 'status_id': 2})

    def test_query_failure_returns_none(self) -> None:
        with patch.object(self.client, 'query', return_value=None):
            self.assertIsNone(self.client.get_user_book('42'))

    def test_rate_limit_returns_none(self) -> None:
        with patch.object(self.client, 'query', side_effect=HardcoverRateLimitError('throttled')):
            self.assertIsNone(self.client.get_user_book('42'))

    def test_user_id_failure_returns_none(self) -> None:
        with patch.object(self.client, 'get_user_id', return_value=None):
            self.assertIsNone(self.client.get_user_book('42'))


if __name__ == '__main__':
    unittest.main()
