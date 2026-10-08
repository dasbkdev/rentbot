"""Transactional SQLite business rules for the rental bot.

Times are UTC Unix seconds. A verified invitation is a one-time credit; ten
credits buy one tenant-request publication, irrespective of subscription status.
The default subscription is 400 KGS for seven days. Payment amounts are stored
as immutable snapshots. Public text and private contact numbers are separate.
"""

import json
import secrets
import sqlite3
import time


class Store:
    """Open a short-lived connection per operation, with atomic write transactions."""

    def __init__(self, path, admin_ids, subscription_days=7, referral_goal=10, clock=time.time):
        self.admin_ids = frozenset(admin_ids)
        self.subscription_days = self._positive_int(subscription_days, "subscription_days")
        self.referral_goal = self._positive_int(referral_goal, "referral_goal")
        self.clock = clock
        self._closed = False
        self._keeper = None
        self._uri = str(path) == ":memory:"
        self.path = ("file:rentbot_" + secrets.token_hex(12) + "?mode=memory&cache=shared"
                     if self._uri else str(path))
        if self._uri:
            self._keeper = self._connect()
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL DEFAULT '',
                    first_name TEXT NOT NULL DEFAULT '',
                    referrer_id INTEGER REFERENCES users(user_id),
                    created_at INTEGER NOT NULL,
                    referral_verified_at INTEGER,
                    subscription_until INTEGER NOT NULL DEFAULT 0,
                    CHECK (referrer_id IS NULL OR referrer_id != user_id)
                );
                CREATE INDEX IF NOT EXISTS users_referrer ON users(referrer_id);
                CREATE TABLE IF NOT EXISTS listings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_by INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    photos TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                    created_at INTEGER NOT NULL,
                    channel_message_id INTEGER,
                    published_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id),
                    receipt_file_id TEXT NOT NULL UNIQUE,
                    receipt_kind TEXT NOT NULL CHECK(receipt_kind IN ('photo', 'document')),
                    amount INTEGER NOT NULL CHECK(amount > 0),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending', 'approved', 'rejected')),
                    created_at INTEGER NOT NULL,
                    decided_at INTEGER,
                    decided_by INTEGER
                );
                CREATE UNIQUE INDEX IF NOT EXISTS payments_one_pending_user
                    ON payments(user_id) WHERE status = 'pending';
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(user_id),
                    rooms INTEGER NOT NULL CHECK(rooms BETWEEN 1 AND 5),
                    district TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    referral_cost INTEGER NOT NULL CHECK(referral_cost > 0),
                    created_at INTEGER NOT NULL,
                    channel_message_id INTEGER,
                    published_at INTEGER
                );
                CREATE UNIQUE INDEX IF NOT EXISTS requests_one_unpublished_user
                    ON requests(user_id) WHERE channel_message_id IS NULL;
                CREATE TABLE IF NOT EXISTS user_states (
                    user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                    state TEXT NOT NULL,
                    data TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_context (
                    user_id INTEGER PRIMARY KEY REFERENCES users(user_id),
                    last_listing_id INTEGER REFERENCES listings(id)
                );
            """)
        finally:
            conn.close()

    def _connect(self):
        if self._closed:
            raise RuntimeError("Store is closed")
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, uri=self._uri)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _run(self, callback, write=False):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            result = callback(conn)
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def close(self):
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None
        self._closed = True

    @staticmethod
    def _positive_int(value, name):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(name + " must be a positive integer")
        return value

    @staticmethod
    def _text(value, name, maximum):
        if not isinstance(value, str):
            raise ValueError(name + " must be text")
        result = value.strip()
        if not result or len(result) > maximum:
            raise ValueError(name + " must contain 1-" + str(maximum) + " characters")
        return result

    @staticmethod
    def _public_text(value):
        # Scan phone-like runs, including formatted and line-wrapped numbers.
        digits = 0
        for char in value:
            if char.isdecimal():
                digits += 1
                if digits >= 7:
                    raise ValueError("Public text must not contain phone numbers; use the contact field")
            elif char.isspace() or char in "+-()./–—":
                continue
            else:
                digits = 0
        return value

    @staticmethod
    def _phone(value):
        if not isinstance(value, str):
            raise ValueError("phone must be text")
        value = value.strip()
        prefix = "+" if value.startswith("+") else ""
        body = value[1:] if prefix else value
        if any(char not in "0123456789-(). " for char in body):
            raise ValueError("phone must contain 7-15 digits and an optional leading +")
        digits = "".join(char for char in body if char in "0123456789")
        if not 7 <= len(digits) <= 15:
            raise ValueError("phone must contain 7-15 digits and an optional leading +")
        return prefix + digits

    def _admin(self, admin_id):
        if isinstance(admin_id, bool) or admin_id not in self.admin_ids:
            raise PermissionError("Administrator permission required")

    @staticmethod
    def _row(row, kind=None):
        if row is None:
            return None
        value = dict(row)
        if kind == "listing":
            value["photos"] = json.loads(value["photos"])
            value["active"] = bool(value["active"])
        return value

    @staticmethod
    def _require_user(conn, user_id):
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            raise ValueError("User is not registered")
        return row

    def register_user(self, user_id, username="", first_name="", referrer_id=None):
        self._positive_int(user_id, "user_id")
        if not isinstance(username, str) or not isinstance(first_name, str):
            raise ValueError("username and first_name must be text")
        if referrer_id is not None:
            self._positive_int(referrer_id, "referrer_id")
            if referrer_id == user_id:
                raise ValueError("Self-referral is not allowed")

        def action(conn):
            existing = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if existing is not None:
                if referrer_id is not None and referrer_id != existing["referrer_id"]:
                    raise ValueError("An existing user's referrer cannot change")
                conn.execute("UPDATE users SET username=?,first_name=? WHERE user_id=?",
                             (username, first_name, user_id))
                return {"created": False, "user": dict(self._require_user(conn, user_id))}
            if referrer_id is not None:
                self._require_user(conn, referrer_id)
            conn.execute("INSERT INTO users(user_id,username,first_name,referrer_id,created_at) VALUES(?,?,?,?,?)",
                         (user_id, username, first_name, referrer_id, int(self.clock())))
            return {"created": True, "user": dict(self._require_user(conn, user_id))}

        return self._run(action, write=True)

    def get_user(self, user_id):
        return self._run(lambda conn: self._row(conn.execute(
            "SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()))

    def create_listing(self, admin_id, description, phone, photos):
        self._admin(admin_id)
        description = self._public_text(self._text(description, "description", 3500))
        phone = self._phone(phone)
        if not isinstance(photos, list) or not 1 <= len(photos) <= 10:
            raise ValueError("Provide 1-10 photos")
        photos = [self._text(photo, "photo file id", 1024) for photo in photos]

        def action(conn):
            cursor = conn.execute("INSERT INTO listings(created_by,description,phone,photos,created_at) VALUES(?,?,?,?,?)",
                                  (admin_id, description, phone, json.dumps(photos), int(self.clock())))
            return self._row(conn.execute("SELECT * FROM listings WHERE id=?", (cursor.lastrowid,)).fetchone(), "listing")

        return self._run(action, write=True)

    def get_listing(self, listing_id):
        return self._run(lambda conn: self._row(conn.execute(
            "SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone(), "listing"))

    def list_listings(self, limit=20, include_inactive=False, offset=0):
        self._positive_int(limit, "limit")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        sql = "SELECT * FROM listings" + ("" if include_inactive else " WHERE active=1")
        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        return self._run(lambda conn: [self._row(row, "listing") for row in conn.execute(sql, (limit, offset))])

    def set_listing_status(self, admin_id, listing_id, active):
        self._admin(admin_id)
        if not isinstance(active, bool):
            raise ValueError("active must be boolean")

        def action(conn):
            cursor = conn.execute("UPDATE listings SET active=? WHERE id=?", (int(active), listing_id))
            if not cursor.rowcount:
                raise ValueError("Listing does not exist")
            return self._row(conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone(), "listing")

        return self._run(action, write=True)

    def set_listing_published(self, admin_id, listing_id, channel_message_id):
        self._admin(admin_id)
        self._mark_published("listings", listing_id, channel_message_id)

    def _mark_published(self, table, item_id, message_id):
        self._positive_int(message_id, "message_id")
        # Table names are exclusively supplied by the two internal call sites.
        if table not in ("listings", "requests"):
            raise ValueError("Invalid publication type")

        def action(conn):
            current = conn.execute("SELECT channel_message_id FROM " + table + " WHERE id=?", (item_id,)).fetchone()
            if current is None:
                raise ValueError("Publication does not exist")
            if current["channel_message_id"] is not None:
                if current["channel_message_id"] != message_id:
                    raise ValueError("Publication already recorded with another message id")
                return
            conn.execute("UPDATE " + table + " SET channel_message_id=?,published_at=? WHERE id=?",
                         (message_id, int(self.clock()), item_id))

        self._run(action, write=True)

    def create_payment(self, user_id, receipt_file_id, receipt_kind, amount=400):
        receipt_file_id = self._text(receipt_file_id, "receipt_file_id", 1024)
        if receipt_kind not in ("photo", "document"):
            raise ValueError("receipt_kind must be photo or document")
        self._positive_int(amount, "amount")

        def action(conn):
            self._require_user(conn, user_id)
            pending = conn.execute("SELECT * FROM payments WHERE user_id=? AND status='pending'", (user_id,)).fetchone()
            if pending is not None:
                return dict(pending)
            existing = conn.execute("SELECT * FROM payments WHERE receipt_file_id=?", (receipt_file_id,)).fetchone()
            if existing is not None:
                if existing["user_id"] != user_id:
                    raise ValueError("Receipt has already been submitted by another user")
                return dict(existing)
            cursor = conn.execute("INSERT INTO payments(user_id,receipt_file_id,receipt_kind,amount,created_at) VALUES(?,?,?,?,?)",
                                  (user_id, receipt_file_id, receipt_kind, amount, int(self.clock())))
            return dict(conn.execute("SELECT * FROM payments WHERE id=?", (cursor.lastrowid,)).fetchone())

        return self._run(action, write=True)

    def get_payment(self, payment_id):
        return self._run(lambda conn: self._row(conn.execute(
            "SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()))

    def list_payments(self, limit=20, status="pending", *, pending_only=None, user_id=None):
        self._positive_int(limit, "limit")
        if pending_only is not None:
            if not isinstance(pending_only, bool):
                raise ValueError("pending_only must be boolean")
            status = "pending" if pending_only else None
        if status not in (None, "pending", "approved", "rejected"):
            raise ValueError("Invalid payment status")
        filters, params = [], []
        if status is not None:
            filters.append("status=?")
            params.append(status)
        if user_id is not None:
            filters.append("user_id=?")
            params.append(user_id)
        sql = "SELECT * FROM payments" + (" WHERE " + " AND ".join(filters) if filters else "")
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self._run(lambda conn: [dict(row) for row in conn.execute(sql, params)])

    def decide_payment(self, admin_id, payment_id, approve):
        self._admin(admin_id)
        if not isinstance(approve, bool):
            raise ValueError("approve must be boolean")

        def action(conn):
            payment = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
            if payment is None:
                raise ValueError("Payment does not exist")
            user = self._require_user(conn, payment["user_id"])
            until = user["subscription_until"]
            if payment["status"] != "pending":
                return {"changed": False, "payment": dict(payment), "subscription_until": until}
            now = int(self.clock())
            if approve:
                until = max(now, until) + self.subscription_days * 86400
                conn.execute("UPDATE users SET subscription_until=? WHERE user_id=?", (until, user["user_id"]))
            conn.execute("UPDATE payments SET status=?,decided_at=?,decided_by=? WHERE id=? AND status='pending'",
                         ("approved" if approve else "rejected", now, admin_id, payment_id))
            payment = conn.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
            return {"changed": True, "payment": dict(payment), "subscription_until": until}

        return self._run(action, write=True)

    def subscription_until(self, user_id):
        user = self.get_user(user_id)
        return user["subscription_until"] if user is not None else 0

    def has_access(self, user_id):
        return self.subscription_until(user_id) > int(self.clock())

    def verify_referral(self, user_id):
        """Caller must first verify that this referred user joined the channel."""
        def action(conn):
            user = self._require_user(conn, user_id)
            if user["referrer_id"] is None or user["referral_verified_at"] is not None:
                return False
            conn.execute("UPDATE users SET referral_verified_at=? WHERE user_id=? AND referral_verified_at IS NULL",
                         (int(self.clock()), user_id))
            return True

        return self._run(action, write=True)

    def _referral_stats(self, conn, user_id):
        total = conn.execute("SELECT COUNT(*) FROM users WHERE referrer_id=? AND referral_verified_at IS NOT NULL",
                             (user_id,)).fetchone()[0]
        used = conn.execute("SELECT COALESCE(SUM(referral_cost),0) FROM requests WHERE user_id=?", (user_id,)).fetchone()[0]
        return {"total": total, "used": used, "available": total - used, "required": self.referral_goal}

    def referral_stats(self, user_id):
        return self._run(lambda conn: self._referral_stats(conn, user_id))

    def create_request(self, user_id, rooms, district, phone):
        if isinstance(rooms, bool) or not isinstance(rooms, int) or not 1 <= rooms <= 5:
            raise ValueError("rooms must be 1-5 (5 means 5 or more)")
        district = self._public_text(self._text(district, "district", 200))
        phone = self._phone(phone)

        def action(conn):
            self._require_user(conn, user_id)
            existing = conn.execute("SELECT * FROM requests WHERE user_id=? AND channel_message_id IS NULL", (user_id,)).fetchone()
            if existing is not None:
                return dict(existing)
            stats = self._referral_stats(conn, user_id)
            if stats["available"] < self.referral_goal:
                raise ValueError("Not enough verified referrals")
            cursor = conn.execute("INSERT INTO requests(user_id,rooms,district,phone,referral_cost,created_at) VALUES(?,?,?,?,?,?)",
                                  (user_id, rooms, district, phone, self.referral_goal, int(self.clock())))
            return dict(conn.execute("SELECT * FROM requests WHERE id=?", (cursor.lastrowid,)).fetchone())

        return self._run(action, write=True)

    def list_requests(self, limit=20, pending_only=False):
        self._positive_int(limit, "limit")
        sql = "SELECT * FROM requests" + (" WHERE channel_message_id IS NULL" if pending_only else "")
        sql += " ORDER BY id DESC LIMIT ?"
        return self._run(lambda conn: [dict(row) for row in conn.execute(sql, (limit,))])

    def get_request(self, request_id):
        return self._run(lambda conn: self._row(conn.execute(
            "SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()))

    def get_pending_request(self, user_id):
        return self._run(lambda conn: self._row(conn.execute(
            "SELECT * FROM requests WHERE user_id=? AND channel_message_id IS NULL", (user_id,)).fetchone()))

    def mark_request_published(self, request_id, message_id):
        self._mark_published("requests", request_id, message_id)

    def get_state(self, user_id):
        def action(conn):
            row = conn.execute("SELECT state,data FROM user_states WHERE user_id=?", (user_id,)).fetchone()
            return {"state": row["state"], "data": json.loads(row["data"])} if row is not None else None

        return self._run(action)

    def set_state(self, user_id, state, data=None):
        if state is not None:
            state = self._text(state, "state", 128)
            if data is not None and not isinstance(data, dict):
                raise ValueError("State data must be a dict")
            try:
                encoded = json.dumps(data if data is not None else {}, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("State data must contain JSON-serializable values") from exc

        def action(conn):
            self._require_user(conn, user_id)
            if state is None:
                conn.execute("DELETE FROM user_states WHERE user_id=?", (user_id,))
            else:
                conn.execute("INSERT INTO user_states(user_id,state,data,updated_at) VALUES(?,?,?,?) "
                             "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state,data=excluded.data,updated_at=excluded.updated_at",
                             (user_id, state, encoded, int(self.clock())))

        self._run(action, write=True)

    def get_last_listing(self, user_id):
        def action(conn):
            row = conn.execute("SELECT last_listing_id FROM user_context WHERE user_id=?", (user_id,)).fetchone()
            return row["last_listing_id"] if row is not None else None

        return self._run(action)

    def set_last_listing(self, user_id, listing_id):
        def action(conn):
            self._require_user(conn, user_id)
            if conn.execute("SELECT id FROM listings WHERE id=?", (listing_id,)).fetchone() is None:
                raise ValueError("Listing does not exist")
            conn.execute("INSERT INTO user_context(user_id,last_listing_id) VALUES(?,?) "
                         "ON CONFLICT(user_id) DO UPDATE SET last_listing_id=excluded.last_listing_id", (user_id, listing_id))

        self._run(action, write=True)
