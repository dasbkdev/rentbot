"""Business invariants, restart recovery, and genuine concurrent write tests."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from rentbot.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "bot.sqlite3"
        self.now = [1_800_000_000]
        self.store = Store(self.path, {900}, clock=lambda: self.now[0])
        self.store.register_user(1, "renter", "Tenant")

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def referrals(self, count=10, start=100):
        for user_id in range(start, start + count):
            self.store.register_user(user_id, referrer_id=1)
            self.store.verify_referral(user_id)

    def listing(self):
        return self.store.create_listing(900, "Двухкомнатная квартира, центр, 35 000 сом", "+996 555 12 34 56", ["photo-1"])

    def test_subscription_starts_on_approval_and_expires_at_exact_boundary(self):
        self.assertFalse(self.store.has_access(1))
        payment = self.store.create_payment(1, "receipt-a", "photo")
        self.now[0] += 7200
        result = self.store.decide_payment(900, payment["id"], True)
        self.assertTrue(result["changed"])
        self.assertEqual(result["subscription_until"], self.now[0] + 7 * 86400)
        self.assertTrue(self.store.has_access(1))
        self.now[0] = result["subscription_until"] - 1
        self.assertTrue(self.store.has_access(1))
        self.now[0] += 1
        self.assertFalse(self.store.has_access(1))
        self.assertEqual(self.store.subscription_until(99999), 0)
        self.assertFalse(self.store.has_access(99999))

    def test_renewal_extends_active_time_and_expired_renewal_starts_today(self):
        first = self.store.create_payment(1, "receipt-a", "photo")
        until = self.store.decide_payment(900, first["id"], True)["subscription_until"]
        self.now[0] += 3600
        second = self.store.create_payment(1, "receipt-b", "document")
        renewed = self.store.decide_payment(900, second["id"], True)["subscription_until"]
        self.assertEqual(renewed, until + 7 * 86400)
        self.now[0] = renewed + 86400
        third = self.store.create_payment(1, "receipt-c", "photo")
        self.assertEqual(self.store.decide_payment(900, third["id"], True)["subscription_until"], self.now[0] + 7 * 86400)

    def test_pending_repeated_upload_keeps_original_receipt_and_amount(self):
        first = self.store.create_payment(1, "receipt-a", "photo", amount=400)
        again = self.store.create_payment(1, "receipt-b", "document", amount=999)
        self.assertEqual(first, again)
        self.assertEqual(first["amount"], 400)
        self.assertEqual(len(self.store.list_payments()), 1)

    def test_decision_replay_cannot_extend_or_reverse_a_decision(self):
        payment = self.store.create_payment(1, "receipt-a", "photo")
        first = self.store.decide_payment(900, payment["id"], True)
        self.now[0] += 100
        for approve in (True, False):
            again = self.store.decide_payment(900, payment["id"], approve)
            self.assertFalse(again["changed"])
            self.assertEqual(again["subscription_until"], first["subscription_until"])
            self.assertEqual(again["payment"]["status"], "approved")

    def test_rejected_payment_never_grants_access_and_fresh_receipt_can_be_submitted(self):
        payment = self.store.create_payment(1, "bad-receipt", "document")
        self.store.decide_payment(900, payment["id"], False)
        self.assertFalse(self.store.has_access(1))
        result = self.store.decide_payment(900, payment["id"], True)
        self.assertFalse(result["changed"])
        self.assertEqual(result["payment"]["status"], "rejected")
        fresh = self.store.create_payment(1, "fresh-receipt", "photo")
        self.assertNotEqual(fresh["id"], payment["id"])
        self.assertEqual(fresh["status"], "pending")

    def test_exact_receipt_replay_after_approval_is_not_a_new_payment(self):
        payment = self.store.create_payment(1, "receipt-a", "photo")
        approved = self.store.decide_payment(900, payment["id"], True)["payment"]
        self.assertEqual(self.store.create_payment(1, "receipt-a", "photo"), approved)
        self.store.register_user(2)
        with self.assertRaises(ValueError):
            self.store.create_payment(2, "receipt-a", "photo")
        self.assertFalse(self.store.has_access(2))

    def test_concurrent_submission_creates_exactly_one_pending(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            payments = list(pool.map(lambda n: self.store.create_payment(1, "receipt-" + str(n), "photo"), range(16)))
        self.assertEqual(len({payment["id"] for payment in payments}), 1)
        self.assertEqual(len(self.store.list_payments()), 1)

    def test_concurrent_approval_grants_exactly_one_week(self):
        payment = self.store.create_payment(1, "receipt-a", "photo")
        with ThreadPoolExecutor(max_workers=8) as pool:
            decisions = list(pool.map(lambda _: self.store.decide_payment(900, payment["id"], True), range(16)))
        self.assertEqual(sum(result["changed"] for result in decisions), 1)
        self.assertEqual(self.store.subscription_until(1), self.now[0] + 7 * 86400)

    def test_independent_store_instances_serialize_approval(self):
        second_store = Store(self.path, {900}, clock=lambda: self.now[0])
        payment = self.store.create_payment(1, "receipt-a", "photo")
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(store.decide_payment, 900, payment["id"], True) for store in (self.store, second_store)]
                results = [future.result() for future in futures]
            self.assertEqual(sum(result["changed"] for result in results), 1)
            self.assertEqual(second_store.subscription_until(1), self.now[0] + 7 * 86400)
        finally:
            second_store.close()

    def test_referrals_only_count_once_after_membership_verification(self):
        self.store.register_user(2, referrer_id=1)
        self.assertEqual(self.store.referral_stats(1), {"total": 0, "used": 0, "available": 0, "required": 10})
        self.assertTrue(self.store.verify_referral(2))
        self.assertFalse(self.store.verify_referral(2))
        self.assertFalse(self.store.verify_referral(1))
        self.assertEqual(self.store.referral_stats(1)["total"], 1)
        self.store.register_user(2, "newname", "New Name")
        self.assertEqual(self.store.get_user(2)["referrer_id"], 1)
        self.assertEqual(self.store.referral_stats(1)["total"], 1)

    def test_self_nonexistent_existing_and_cyclic_referrals_rejected(self):
        with self.assertRaises(ValueError):
            self.store.register_user(2, referrer_id=2)
        with self.assertRaises(ValueError):
            self.store.register_user(2, referrer_id=999)
        self.assertIsNone(self.store.get_user(2))
        self.store.register_user(2, referrer_id=1)
        with self.assertRaises(ValueError):
            self.store.register_user(1, referrer_id=2)
        self.store.register_user(3)
        with self.assertRaises(ValueError):
            self.store.register_user(3, referrer_id=1)
        with self.assertRaises(ValueError):
            self.store.register_user(2, referrer_id=3)
        self.assertIsNone(self.store.get_user(1)["referrer_id"])
        self.assertIsNone(self.store.get_user(3)["referrer_id"])
        self.assertEqual(self.store.get_user(2)["referrer_id"], 1)

    def test_requests_require_verified_referrals_and_spend_exactly_ten(self):
        for uid in range(100, 110):
            self.store.register_user(uid, referrer_id=1)
        with self.assertRaises(ValueError):
            self.store.create_request(1, 2, "Центр", "+996555123456")
        for uid in range(100, 110):
            self.store.verify_referral(uid)
        request = self.store.create_request(1, 2, "Центр", "+996555123456")
        self.assertEqual(request["referral_cost"], 10)
        self.assertEqual(self.store.referral_stats(1), {"total": 10, "used": 10, "available": 0, "required": 10})
        self.store.mark_request_published(request["id"], 123)
        with self.assertRaises(ValueError):
            self.store.create_request(1, 1, "Юг", "+996555123456")
        self.referrals(10, start=200)
        next_request = self.store.create_request(1, 1, "Юг", "+996555123456")
        self.assertNotEqual(request["id"], next_request["id"])
        self.assertEqual(self.store.referral_stats(1)["used"], 20)

    def test_delivery_failure_retry_returns_same_request_without_double_spending(self):
        self.referrals(20)
        self.assertIsNone(self.store.get_pending_request(1))
        original = self.store.create_request(1, 2, "Центр", "+996555123456")
        self.assertEqual(self.store.get_pending_request(1), original)
        self.assertEqual(self.store.create_request(1, 5, "Другой район", "+996777123456"), original)
        self.assertEqual(self.store.referral_stats(1)["available"], 10)
        self.assertEqual(self.store.list_requests(pending_only=True), [original])
        self.store.mark_request_published(original["id"], 123)
        self.store.mark_request_published(original["id"], 123)
        self.assertIsNone(self.store.get_pending_request(1))
        self.assertEqual(self.store.list_requests(pending_only=True), [])
        with self.assertRaises(ValueError):
            self.store.mark_request_published(original["id"], 124)

    def test_concurrent_requests_reserve_credits_once(self):
        self.referrals(20)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.create_request(1, 2, "Центр", "+996555123456"), range(16)))
        self.assertEqual(len({result["id"] for result in results}), 1)
        self.assertEqual(self.store.referral_stats(1)["used"], 10)

    def test_concurrent_verification_cannot_multiply_invitation_credits(self):
        self.store.register_user(2, referrer_id=1)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.verify_referral(2), range(16)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(self.store.referral_stats(1)["total"], 1)

    def test_administrator_guard_applies_to_every_admin_mutation(self):
        listing = self.listing()
        payment = self.store.create_payment(1, "receipt-a", "photo")
        denied = [
            lambda: self.store.create_listing(1, "Квартира", "+996555123456", ["photo"]),
            lambda: self.store.set_listing_status(1, listing["id"], False),
            lambda: self.store.set_listing_published(1, listing["id"], 10),
            lambda: self.store.decide_payment(1, payment["id"], True),
        ]
        for operation in denied:
            with self.assertRaises(PermissionError):
                operation()
        self.assertTrue(self.store.get_listing(listing["id"])["active"])
        self.assertEqual(self.store.get_payment(payment["id"])["status"], "pending")

    def test_phone_stays_separate_from_public_description(self):
        listing = self.listing()
        self.assertEqual(listing["phone"], "+996555123456")
        self.assertNotIn(listing["phone"], listing["description"])
        for text in ("Звоните 996555123456", "Контакт +996 (555) 12-34-56", "Тел 1234\n5678", "12345678", "1234567", "Тел. 123-45-67", "１２３４５６７８"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.store.create_listing(900, text, "+996555123456", ["photo"])
        with self.assertRaises(ValueError):
            self.store.create_request(1, 2, "Центр, звоните 0555123456", "+996555123456")

    def test_input_validation_rolls_back_without_partial_rows(self):
        for phone in ("", "123456", "1" * 16, "++996555123456", "+996CALL123456", "123; DROP TABLE users"):
            with self.subTest(phone=phone), self.assertRaises(ValueError):
                self.store.create_listing(900, "Квартира", phone, ["photo"])
        for description in ("", " ", "a" * 3501):
            with self.assertRaises(ValueError):
                self.store.create_listing(900, description, "1234567", ["photo"])
        for photos in ([], ["p"] * 11, [""], "photo"):
            with self.assertRaises(ValueError):
                self.store.create_listing(900, "Квартира", "1234567", photos)
        for rooms in (0, 6, True, "2"):
            with self.assertRaises(ValueError):
                self.store.create_request(1, rooms, "Центр", "1234567")
        for amount in (0, -400, True, 400.5):
            with self.assertRaises(ValueError):
                self.store.create_payment(1, "receipt", "photo", amount)
        self.assertEqual(self.store.list_listings(), [])
        self.assertEqual(self.store.list_payments(), [])

    def test_listing_status_publish_and_last_listing_independent_of_fsm(self):
        listing = self.listing()
        self.store.set_state(1, "waiting_receipt", {"step": 2})
        self.store.set_last_listing(1, listing["id"])
        self.assertEqual(self.store.get_last_listing(1), listing["id"])
        self.assertEqual(self.store.get_state(1), {"state": "waiting_receipt", "data": {"step": 2}})
        self.store.set_listing_published(900, listing["id"], 30)
        self.store.set_listing_published(900, listing["id"], 30)
        self.assertEqual(self.store.get_listing(listing["id"])["channel_message_id"], 30)
        self.store.set_listing_status(900, listing["id"], False)
        self.assertEqual(self.store.list_listings(), [])
        self.assertEqual(len(self.store.list_listings(include_inactive=True)), 1)
        self.store.set_listing_status(900, listing["id"], True)
        self.assertEqual(len(self.store.list_listings()), 1)

    def test_persistence_preserves_receipts_credits_publication_and_draft(self):
        self.referrals()
        listing = self.listing()
        self.store.set_last_listing(1, listing["id"])
        self.store.set_state(1, "request_confirm", {"district": "Центр", "photos": ["file"]})
        payment = self.store.create_payment(1, "receipt", "photo")
        self.store.decide_payment(900, payment["id"], True)
        request = self.store.create_request(1, 2, "Центр", "+996555123456")
        self.store.close()
        self.store = Store(self.path, {900}, clock=lambda: self.now[0])
        self.assertTrue(self.store.has_access(1))
        self.assertEqual(self.store.get_payment(payment["id"])["status"], "approved")
        self.assertEqual(self.store.referral_stats(1)["used"], 10)
        self.assertEqual(self.store.get_state(1)["data"]["district"], "Центр")
        self.assertEqual(self.store.get_last_listing(1), listing["id"])
        self.assertEqual(self.store.create_request(1, 2, "Центр", "+996555123456"), request)
        self.store.set_state(1, None)
        self.assertIsNone(self.store.get_state(1))

    def test_payment_recovery_filters(self):
        self.store.register_user(2)
        first = self.store.create_payment(1, "receipt-1", "photo")
        second = self.store.create_payment(2, "receipt-2", "document")
        self.store.decide_payment(900, first["id"], True)
        self.assertEqual(self.store.list_payments(), [second])
        self.assertEqual(self.store.list_payments(status="approved")[0]["id"], first["id"])
        self.assertEqual(len(self.store.list_payments(pending_only=False)), 2)
        self.assertEqual(len(self.store.list_payments(pending_only=False, user_id=1)), 1)

    def test_listing_pagination_has_stable_order_filters_before_offset_and_rejects_bad_offset(self):
        listings = [self.listing() for _ in range(5)]
        self.store.set_listing_status(900, listings[3]["id"], False)
        first = self.store.list_listings(limit=2)
        second = self.store.list_listings(limit=2, offset=2)
        self.assertEqual([item["id"] for item in first], [listings[4]["id"], listings[2]["id"]])
        self.assertEqual([item["id"] for item in second], [listings[1]["id"], listings[0]["id"]])
        self.assertEqual(self.store.list_listings(limit=2, offset=4), [])
        all_second = self.store.list_listings(limit=2, include_inactive=True, offset=2)
        self.assertEqual([item["id"] for item in all_second], [listings[2]["id"], listings[1]["id"]])
        for offset in (-1, True, 1.5, "2", None):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.store.list_listings(offset=offset)

    def test_missing_records_and_serialization_errors_do_not_change_state(self):
        self.assertIsNone(self.store.get_payment(999))
        self.assertIsNone(self.store.get_listing(999))
        self.assertIsNone(self.store.get_request(999))
        with self.assertRaises(ValueError):
            self.store.decide_payment(900, 999, True)
        with self.assertRaises(ValueError):
            self.store.create_payment(999, "receipt", "photo")
        self.store.set_state(1, "draft", {"keep": True})
        with self.assertRaises(ValueError):
            self.store.set_state(1, "broken", {"bad": object()})
        self.assertEqual(self.store.get_state(1), {"state": "draft", "data": {"keep": True}})

    def test_memory_store_survives_independent_operation_connections(self):
        memory = Store(":memory:", {900}, clock=lambda: self.now[0])
        try:
            memory.register_user(1)
            memory.set_state(1, "draft", {"text": "test"})
            self.assertEqual(memory.get_state(1)["data"]["text"], "test")
        finally:
            memory.close()


if __name__ == "__main__":
    unittest.main()
