"""
Fund ticker -> human-readable name mapping for spot BTC and ETH ETFs.
"""

FUND_NAMES = {
    # BTC spot ETFs
    "IBIT": "iShares Bitcoin Trust (BlackRock)",
    "FBTC": "Fidelity Wise Origin Bitcoin Fund",
    "BITB": "Bitwise Bitcoin ETF",
    "ARKB": "ARK 21Shares Bitcoin ETF",
    "BTCO": "Invesco Galaxy Bitcoin ETF",
    "EZBC": "Franklin Bitcoin ETF",
    "BRRR": "Valkyrie Bitcoin Fund",
    "HODL": "VanEck Bitcoin Trust",
    "BTCW": "WisdomTree Bitcoin Fund",
    "MSBT": "Morgan Stanley Bitcoin Trust",
    "GBTC": "Grayscale Bitcoin Trust",
    "BTC":  "Grayscale Bitcoin Mini Trust",

    # ETH spot ETFs
    "ETHA": "iShares Ethereum Trust (BlackRock)",
    "FETH": "Fidelity Ethereum Fund",
    "ETHW": "Bitwise Ethereum ETF",
    "CETH": "21Shares Core Ethereum ETF",
    "ETHV": "VanEck Ethereum ETF",
    "QETH": "Invesco Galaxy Ethereum ETF",
    "EZET": "Franklin Ethereum ETF",
    "ETHE": "Grayscale Ethereum Trust",
    "ETH_MINI": "Grayscale Ethereum Mini Trust",
}


def name_for(symbol: str) -> str:
    """Return the canonical name for a ticker symbol, or the symbol itself if unknown."""
    if not symbol:
        return ""
    return FUND_NAMES.get(symbol.upper(), FUND_NAMES.get(symbol, symbol))
