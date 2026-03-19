import base64
import unittest
import uuid

import utils as tu
from common.constants import (
    CMD_COMMIT,
    CMD_HOLD,
    CMD_RELEASE,
    PAYMENT_COMMANDS_DLQ,
    PAYMENT_COMMANDS_QUEUE,
    STOCK_COMMANDS_DLQ,
    STOCK_COMMANDS_QUEUE,
)
from common.models import ParticipantCommand, PaymentHoldPayload, StockHoldPayload
from live_stack_utils import (
    LiveStackTestCase,
    get_queue_messages,
    publish_base64,
    publish_command,
    purge_queue,
    queue_message_count,
    run_compose,
    temporary_reply_queue,
    wait_for_queue_consumers,
    wait_gateway_ready,
    wait_for_queue_messages,
    wait_for_replies,
)


class TestParticipantWorkerIntegration(LiveStackTestCase):
    def setUp(self):
        for service_name in ("order-service", "orchestrator-service", "stock-service", "payment-service"):
            # start is idempotent and cheaper than full restart for test setup
            run_compose("start", service_name)
            wait_gateway_ready()

        for queue_name in (
            STOCK_COMMANDS_QUEUE,
            PAYMENT_COMMANDS_QUEUE,
            STOCK_COMMANDS_DLQ,
            PAYMENT_COMMANDS_DLQ,
        ):
            purge_queue(queue_name)

        wait_for_queue_consumers(STOCK_COMMANDS_QUEUE, minimum=1, timeout_seconds=30.0)
        wait_for_queue_consumers(PAYMENT_COMMANDS_QUEUE, minimum=1, timeout_seconds=30.0)

    def test_stock_hold_and_release_replays_are_idempotent(self):
        item_id = tu.create_item(9)["item_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_stock(item_id, 12)))

        tx_id = f"stock-replay-{uuid.uuid4().hex[:10]}"
        hold_command = ParticipantCommand(
            tx_id=tx_id,
            command=CMD_HOLD,
            reply_to="",
            stock_payload=StockHoldPayload(items=[(item_id, 4)]),
        )
        release_command = ParticipantCommand(
            tx_id=tx_id,
            command=CMD_RELEASE,
            reply_to="",
            stock_payload=StockHoldPayload(items=[(item_id, 4)]),
        )

        with temporary_reply_queue() as reply_queue:
            hold_command.reply_to = reply_queue
            release_command.reply_to = reply_queue

            publish_command(STOCK_COMMANDS_QUEUE, hold_command)
            first_hold = wait_for_replies(reply_queue, expected_count=1)[0]
            self.assertTrue(first_hold.ok)
            self.assertEqual(first_hold.command, CMD_HOLD)
            self.assertEqual(tu.find_item(item_id)["stock"], 8)

            publish_command(STOCK_COMMANDS_QUEUE, hold_command)
            second_hold = wait_for_replies(reply_queue, expected_count=1)[0]
            self.assertTrue(second_hold.ok)
            self.assertEqual(second_hold.command, CMD_HOLD)
            self.assertEqual(
                tu.find_item(item_id)["stock"],
                8,
                "Replaying the same stock hold tx_id must not subtract twice",
            )

            publish_command(STOCK_COMMANDS_QUEUE, release_command)
            first_release = wait_for_replies(reply_queue, expected_count=1)[0]
            self.assertTrue(first_release.ok)
            self.assertEqual(first_release.command, CMD_RELEASE)
            self.assertEqual(tu.find_item(item_id)["stock"], 12)

            publish_command(STOCK_COMMANDS_QUEUE, release_command)
            second_release = wait_for_replies(reply_queue, expected_count=1)[0]
            self.assertTrue(second_release.ok)
            self.assertEqual(second_release.command, CMD_RELEASE)
            self.assertEqual(
                tu.find_item(item_id)["stock"],
                12,
                "Replaying the same stock release tx_id must not restore twice",
            )

    def test_payment_hold_and_release_replays_are_idempotent(self):
        user_id = tu.create_user()["user_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_credit_to_user(user_id, 30)))

        tx_id = f"payment-replay-{uuid.uuid4().hex[:10]}"
        hold_command = ParticipantCommand(
            tx_id=tx_id,
            command=CMD_HOLD,
            reply_to="",
            payment_payload=PaymentHoldPayload(user_id=user_id, amount=11),
        )
        release_command = ParticipantCommand(
            tx_id=tx_id,
            command=CMD_RELEASE,
            reply_to="",
            payment_payload=PaymentHoldPayload(user_id=user_id, amount=11),
        )

        with temporary_reply_queue() as reply_queue:
            hold_command.reply_to = reply_queue
            release_command.reply_to = reply_queue

            publish_command(PAYMENT_COMMANDS_QUEUE, hold_command)
            first_hold = wait_for_replies(reply_queue, expected_count=1)[0]
            self.assertTrue(first_hold.ok)
            self.assertEqual(first_hold.command, CMD_HOLD)
            self.assertEqual(tu.find_user(user_id)["credit"], 19)

            publish_command(PAYMENT_COMMANDS_QUEUE, hold_command)
            second_hold = wait_for_replies(reply_queue, expected_count=1)[0]
            self.assertTrue(second_hold.ok)
            self.assertEqual(second_hold.command, CMD_HOLD)
            self.assertEqual(
                tu.find_user(user_id)["credit"],
                19,
                "Replaying the same payment hold tx_id must not charge twice",
            )

            publish_command(PAYMENT_COMMANDS_QUEUE, release_command)
            first_release = wait_for_replies(reply_queue, expected_count=1)[0]
            self.assertTrue(first_release.ok)
            self.assertEqual(first_release.command, CMD_RELEASE)
            self.assertEqual(tu.find_user(user_id)["credit"], 30)

            publish_command(PAYMENT_COMMANDS_QUEUE, release_command)
            second_release = wait_for_replies(reply_queue, expected_count=1)[0]
            self.assertTrue(second_release.ok)
            self.assertEqual(second_release.command, CMD_RELEASE)
            self.assertEqual(
                tu.find_user(user_id)["credit"],
                30,
                "Replaying the same payment release tx_id must not refund twice",
            )

    def test_malformed_participant_messages_are_dead_lettered(self):
        malformed_payload = base64.b64encode(b"not-a-valid-msgpack-command").decode()

        for queue_name, dlq_name in (
            (STOCK_COMMANDS_QUEUE, STOCK_COMMANDS_DLQ),
            (PAYMENT_COMMANDS_QUEUE, PAYMENT_COMMANDS_DLQ),
        ):
            with self.subTest(queue=queue_name):
                purge_queue(queue_name)
                purge_queue(dlq_name)

                publish_base64(queue_name, malformed_payload)
                wait_for_queue_messages(dlq_name, minimum=1, timeout_seconds=10.0)
                self.assertGreaterEqual(queue_message_count(dlq_name), 1)

                dlq_messages = get_queue_messages(dlq_name, count=10, requeue=False)
                self.assertTrue(dlq_messages, f"Expected malformed payload in {dlq_name}")
                self.assertEqual(
                    dlq_messages[0]["payload"],
                    malformed_payload,
                    "DLQ should preserve the original malformed payload",
                )

    def test_unknown_tx_commit_and_release_are_noop_successes(self):
        item_id = tu.create_item(4)["item_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_stock(item_id, 7)))
        user_id = tu.create_user()["user_id"]
        self.assertTrue(tu.status_code_is_success(tu.add_credit_to_user(user_id, 18)))

        with temporary_reply_queue() as reply_queue:
            stock_tx_id = f"stock-miss-{uuid.uuid4().hex[:10]}"
            payment_tx_id = f"payment-miss-{uuid.uuid4().hex[:10]}"

            stock_release = ParticipantCommand(
                tx_id=stock_tx_id,
                command=CMD_RELEASE,
                reply_to=reply_queue,
                stock_payload=StockHoldPayload(items=[(item_id, 3)]),
            )
            stock_commit = ParticipantCommand(
                tx_id=stock_tx_id,
                command=CMD_COMMIT,
                reply_to=reply_queue,
                stock_payload=StockHoldPayload(items=[(item_id, 3)]),
            )
            payment_release = ParticipantCommand(
                tx_id=payment_tx_id,
                command=CMD_RELEASE,
                reply_to=reply_queue,
                payment_payload=PaymentHoldPayload(user_id=user_id, amount=9),
            )
            payment_commit = ParticipantCommand(
                tx_id=payment_tx_id,
                command=CMD_COMMIT,
                reply_to=reply_queue,
                payment_payload=PaymentHoldPayload(user_id=user_id, amount=9),
            )

            publish_command(STOCK_COMMANDS_QUEUE, stock_release)
            publish_command(STOCK_COMMANDS_QUEUE, stock_commit)
            publish_command(PAYMENT_COMMANDS_QUEUE, payment_release)
            publish_command(PAYMENT_COMMANDS_QUEUE, payment_commit)

            replies = wait_for_replies(reply_queue, expected_count=4)

        self.assertEqual(len(replies), 4)
        for reply in replies:
            self.assertTrue(reply.ok, f"Expected no-op success, got {reply}")
            self.assertIn(reply.command, {CMD_RELEASE, CMD_COMMIT})

        self.assertEqual(tu.find_item(item_id)["stock"], 7)
        self.assertEqual(tu.find_user(user_id)["credit"], 18)


if __name__ == "__main__":
    unittest.main()
