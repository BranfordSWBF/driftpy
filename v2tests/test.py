import pytest
import asyncio
from pytest import fixture, mark
from pytest_asyncio import fixture as async_fixture
from anchorpy import Provider, WorkspaceType, workspace_fixture, Program
from solana.keypair import Keypair
from solana.transaction import Transaction
from solana.system_program import create_account, CreateAccountParams
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token._layouts import MINT_LAYOUT
from spl.token.async_client import AsyncToken
from spl.token.instructions import initialize_mint, InitializeMintParams

from solana.system_program import create_account, CreateAccountParams
from spl.token.async_client import AsyncToken
from spl.token._layouts import ACCOUNT_LAYOUT
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    initialize_account,
    InitializeAccountParams,
    mint_to,
    MintToParams,
)
from anchorpy import Program, Provider, WorkspaceType
from anchorpy.utils.token import get_token_account
from driftpy.admin import Admin
from driftpy.constants.numeric_constants import MARK_PRICE_PRECISION
from math import sqrt
from typing import cast
from pytest import fixture, mark
from pytest_asyncio import fixture as async_fixture
from solana.keypair import Keypair
from solana.publickey import PublicKey
from solana.transaction import Transaction
from solana.system_program import create_account, CreateAccountParams
from spl.token.async_client import AsyncToken
from spl.token._layouts import ACCOUNT_LAYOUT
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    initialize_account,
    InitializeAccountParams,
    mint_to,
    MintToParams,
)
from anchorpy import Program, Provider, WorkspaceType
from anchorpy.utils.token import get_token_account

from driftpy.admin import Admin
from driftpy.constants.numeric_constants import MARK_PRICE_PRECISION, AMM_RESERVE_PRECISION
from driftpy.clearing_house import ClearingHouse
from driftpy.setup.helpers import _create_usdc_mint, mock_oracle, _create_and_mint_user_usdc

from driftpy.addresses import * 
from driftpy.types import * 
from driftpy.accounts import *

MANTISSA_SQRT_SCALE = int(sqrt(MARK_PRICE_PRECISION))
AMM_INITIAL_QUOTE_ASSET_AMOUNT = int((5 * 10 ** 13) * MANTISSA_SQRT_SCALE)
AMM_INITIAL_BASE_ASSET_AMOUNT = int((5 * 10 ** 13) * MANTISSA_SQRT_SCALE)
PERIODICITY = 60 * 60  # 1 HOUR
USDC_AMOUNT = int(10 * 10 ** 6)
MARKET_INDEX = 0

workspace = workspace_fixture(
    "protocol-v2", build_cmd="anchor build --skip-lint", scope="session"
)

@async_fixture(scope="session")
async def usdc_mint(provider: Provider):
    return await _create_usdc_mint(provider)

@async_fixture(scope="session")
async def user_usdc_account(
    usdc_mint: Keypair,
    provider: Provider,
):
    return await _create_and_mint_user_usdc(
        usdc_mint, 
        provider, 
        USDC_AMOUNT, 
        provider.wallet.public_key
    )

@fixture(scope="session")
def program(workspace: WorkspaceType) -> Program:
    """Create a Program instance."""
    return workspace["clearing_house"]

@fixture(scope="session")
def provider(program: Program) -> Provider:
    return program.provider

@async_fixture(scope="session")
async def clearing_house(program: Program, usdc_mint: Keypair) -> Admin:
    admin = Admin(program)
    await admin.initialize(usdc_mint.public_key, admin_controls_prices=True)
    return admin 

@async_fixture(scope="session")
async def initialized_spot_market(
    clearing_house: Admin, 
    usdc_mint: Keypair,
): 
    await clearing_house.initialize_spot_market(
        usdc_mint.public_key 
    )

@async_fixture(scope="session")
async def initialized_market(
    clearing_house: Admin, workspace: WorkspaceType
) -> PublicKey:
    pyth_program = workspace["pyth"]
    sol_usd = await mock_oracle(pyth_program=pyth_program, price=1)

    await clearing_house.initialize_market(
        sol_usd,
        AMM_INITIAL_BASE_ASSET_AMOUNT,
        AMM_INITIAL_QUOTE_ASSET_AMOUNT,
        PERIODICITY,
    )

    return sol_usd

@mark.asyncio
async def test_spot(
    clearing_house: Admin,
    initialized_spot_market: PublicKey,
):
    program = clearing_house.program
    spot_market = await get_spot_market_account(program, 0)
    assert spot_market.market_index == 0 

@mark.asyncio
async def test_market(
    clearing_house: Admin,
    initialized_market: PublicKey,
):
    program = clearing_house.program
    market_oracle_public_key = initialized_market
    market: PerpMarket = await get_market_account(program, 0)

    assert market.amm.oracle == market_oracle_public_key

@mark.asyncio
async def test_init_user(
    clearing_house: Admin,
):
    await clearing_house.intialize_user()
    user: User = await get_user_account(
        clearing_house.program, 
        clearing_house.authority, 
        user_id=0
    )
    assert user.authority == clearing_house.authority


@mark.asyncio
async def test_usdc_deposit(
    clearing_house: Admin,
    user_usdc_account: Keypair,
):
    await clearing_house.deposit(
        USDC_AMOUNT, 
        0, 
        user_usdc_account.public_key, 
        user_initialized=True
    )
    user_account = await get_user_account(
        clearing_house.program, 
        clearing_house.authority
    )
    assert user_account.spot_positions[0].balance == USDC_AMOUNT

from time import sleep
@mark.asyncio
async def test_add_remove_liquidity(
    clearing_house: Admin,
):
    market = await get_market_account(clearing_house.program, 0)
    n_shares = market.amm.base_asset_amount_step_size

    await clearing_house.update_lp_cooldown_time(0, 0)
    market = await get_market_account(clearing_house.program, 0)
    assert market.amm.lp_cooldown_time == 0

    await clearing_house.add_liquidity(
        n_shares, 
        0
    )
    user_account = await get_user_account(
        clearing_house.program, 
        clearing_house.authority
    )
    assert user_account.perp_positions[0].lp_shares == n_shares

    await clearing_house.settle_lp(
        clearing_house.authority, 
        0
    )

    await clearing_house.remove_liquidity(
        n_shares, 0
    )
    user_account = await get_user_account(
        clearing_house.program, 
        clearing_house.authority
    )
    assert user_account.perp_positions[0].lp_shares == 0

@mark.asyncio
async def test_open_close_position(
    clearing_house: Admin,
):
    await clearing_house.update_perp_auction_duration(
        0
    )

    baa = 10 * AMM_RESERVE_PRECISION
    sig = await clearing_house.open_position(
        PositionDirection.LONG(), 
        baa, 
        0, 
    )
    from solana.rpc.commitment import Confirmed, Processed
    clearing_house.program.provider.connection._commitment = Confirmed
    tx = await clearing_house.program.provider.connection.get_transaction(sig)
    # clearing_house.program.provider.connection._commitment = Processed
    # print(tx)
    
    user_account = await get_user_account(
        clearing_house.program, 
        clearing_house.authority
    )

    assert user_account.perp_positions[0].base_asset_amount == baa
    assert user_account.perp_positions[0].quote_asset_amount < 0

    await clearing_house.close_position(
        0
    )

    user_account = await get_user_account(
        clearing_house.program, 
        clearing_house.authority
    )
    assert user_account.perp_positions[0].base_asset_amount == 0
    assert user_account.perp_positions[0].quote_asset_amount == -20001