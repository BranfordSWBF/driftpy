import asyncio
import time
from solders.hash import Hash
from solders.keypair import Keypair
from driftpy.tx.types import TxSender, TxSigAndSlot
from solana.rpc.async_api import AsyncClient
from solana.rpc.types import TxOpts
from solana.rpc.commitment import Commitment, Confirmed
from typing import Union, Sequence, Optional

from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.instruction import Instruction
from solders.message import MessageV0
from solders.rpc.responses import SendTransactionResp
from solana.transaction import Transaction
from solders.transaction import VersionedTransaction

from perp_dex_mm.utils.logging_utils import get_custom_logger


class StandardTxSender(TxSender):
    def __init__(
        self,
        connection: AsyncClient,
        opts: TxOpts,
        blockhash_commitment: Commitment = Confirmed,
        connections: Optional[list[AsyncClient]] = None,
    ):
        self.logger = get_custom_logger("drift_standard_tx_sender")
        self.connection = connection
        if opts.skip_confirmation:
            raise ValueError("RetryTxSender doesnt support skip confirmation")
        self.connections: list[AsyncClient] | None = connections
        if connections is not None:
            for connection in connections:
                if not isinstance(connection, AsyncClient):
                    raise ValueError(
                        "connections must be a list of AsyncClient"
                    )

        self.opts = opts
        self.blockhash_commitment = blockhash_commitment

    async def get_blockhash(self) -> Hash:
        return (
            await self.connection.get_latest_blockhash(
                self.blockhash_commitment
            )
        ).value.blockhash

    async def fetch_latest_blockhash(self) -> Hash:
        return await self.get_blockhash()

    async def get_legacy_tx(
        self,
        ixs: Sequence[Instruction],
        payer: Keypair,
        additional_signers: Optional[Sequence[Keypair]],
    ) -> Transaction:
        latest_blockhash = await self.fetch_latest_blockhash()
        tx = Transaction(
            instructions=ixs,
            recent_blockhash=latest_blockhash,
            fee_payer=payer.pubkey(),
        )

        tx.sign_partial(payer)

        if additional_signers is not None:
            [tx.sign_partial(signer) for signer in additional_signers]

        return tx

    async def get_versioned_tx(
        self,
        ixs: Sequence[Instruction],
        payer: Keypair,
        lookup_tables: Sequence[AddressLookupTableAccount],
        additional_signers: Optional[Sequence[Keypair]],
    ) -> VersionedTransaction:
        latest_blockhash = await self.fetch_latest_blockhash()

        msg = MessageV0.try_compile(
            payer.pubkey(), ixs, lookup_tables, latest_blockhash
        )

        signers = [payer]
        if additional_signers is not None:
            [signers.append(signer) for signer in additional_signers]

        return VersionedTransaction(msg, signers)

    async def multi_send(
        self, tx: Union[Transaction, VersionedTransaction]
    ) -> TxSigAndSlot:
        # send tx to multiple providers
        if self.connections is None:
            raise ValueError("connections must be provided to use multi_send")
        raw = tx.serialize() if isinstance(tx, Transaction) else bytes(tx)

        body = self.connection._send_raw_transaction_body(raw, self.opts)
        # use asyncio.create_task for high throughput
        start_time = time.time()

        # we only wait for 1 second for all responses, if any of the responses not returned we retry
        make_request_task_by_connection: dict[AsyncClient, asyncio.Task] = {}
        for connection in self.connections:
            make_request_task = asyncio.create_task(
                connection._provider.make_request(body, SendTransactionResp)
            )
            make_request_task_by_connection[connection] = make_request_task
        EXIT_LOOP = False
        while not EXIT_LOOP:
            for (
                connection,
                make_request_task,
            ) in make_request_task_by_connection.items():
                if make_request_task.done():
                    if make_request_task.exception() is not None:
                        self.logger.error(
                            f"Failed to send transaction: {make_request_task.exception()}"
                        )
                        # retry
                        make_request_task_by_connection[connection] = (
                            asyncio.create_task(
                                connection._provider.make_request(
                                    body, SendTransactionResp
                                )
                            )
                        )
                    else:
                        # we can exit the loop once we get the response from one of the connections
                        resp = make_request_task.result()
                        self.logger.info(
                            f"get response from {connection._provider}"
                        )
                        EXIT_LOOP = True
                else:
                    await asyncio.sleep(0.01)
        end_time = time.time()
        self.logger.info(
            f"time taken for multi_send: {end_time - start_time:.2f}"
        )
        try:
            sig_status_task_by_connection = {}
            for connection in self.connections:
                sig_status_task = asyncio.create_task(
                    self.connection.confirm_transaction(
                        resp.value, self.opts.preflight_commitment
                    )
                )
                sig_status_task_by_connection[connection] = sig_status_task
            await asyncio.sleep(0.01)
            while True:
                for (
                    connection,
                    sig_status_task,
                ) in sig_status_task_by_connection.items():
                    if sig_status_task.done():
                        sig_status = sig_status_task.result()
                        slot = sig_status.context.slot
                        self.logger.info(
                            f"get slot from {connection._provider} "
                        )
                        for (
                            _connection,
                            _sig_status_task,
                        ) in sig_status_task_by_connection.items():
                            try:
                                if _connection != connection:
                                    _sig_status_task.cancel()
                            except asyncio.CancelledError:
                                pass
                            except Exception:
                                self.logger.error(
                                    f"Failed to cancel task: {_sig_status_task.exception()}"
                                )
                        return TxSigAndSlot(resp.value, slot)
                await asyncio.sleep(0.01)
        except Exception:
            self.logger.error(f"Failed to confirm transaction: {resp}")

    async def send(
        self, tx: Union[Transaction, VersionedTransaction]
    ) -> TxSigAndSlot:
        raw = tx.serialize() if isinstance(tx, Transaction) else bytes(tx)

        body = self.connection._send_raw_transaction_body(raw, self.opts)
        resp = await self.connection._provider.make_request(
            body, SendTransactionResp
        )
        sig = resp.value

        sig_status = await self.connection.confirm_transaction(
            sig, self.opts.preflight_commitment
        )
        slot = sig_status.context.slot

        return TxSigAndSlot(sig, slot)

    async def send_no_confirm(
        self, tx: Union[Transaction, VersionedTransaction]
    ) -> TxSigAndSlot:
        raw = tx.serialize() if isinstance(tx, Transaction) else bytes(tx)

        body = self.connection._send_raw_transaction_body(raw, self.opts)
        resp = await self.connection._provider.make_request(
            body, SendTransactionResp
        )
        sig = resp.value

        return TxSigAndSlot(sig, 0)
