"""Build the LTHCS universe.json from explicit DJIA 30, NASDAQ-100, and S&P 100
constituent lists.

Run from the repo root:

    .venv/bin/python scripts/lthcs_build_universe.py

The script writes ``data/lthcs/universe.json`` and is the canonical source for
how that file is constructed. It is purely declarative — index memberships and
per-ticker metadata are hardcoded below, then merged and serialized.

Constituent lists reflect late 2025 / early 2026 composition:
  * DJIA 30 — post the 2024 NVDA→INTC and SHW→DOW swaps; DOW Inc. itself was
    removed from the DJIA in 2024 (replaced by SHW) so it is not tagged DJIA.
  * NASDAQ-100 — post-2025 reconstitution (incl. ARM, DDOG, MELI, MDB, etc.).
  * S&P 100 — top ~100 names from S&P 500 by market cap (OEX-tracked).

Every ticker that appears in any of the three indices is implicitly in the
S&P 500 as well (since DJIA 30 ⊂ S&P 500, NDX has heavy overlap with SPX,
and S&P 100 ⊂ S&P 500) — but a small number of NDX names are NOT in the
S&P 500 (foreign-domiciled ADRs like ASML, AZN, ARM, GFS, CCEP and Liberty
Media tracking stocks). Those are kept as NDX-only.

Maturity-stage overrides preserve the existing V1 universe tagging.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Dict, List, Set, Tuple


REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "data" / "lthcs" / "universe.json"


# ---------------------------------------------------------------------------
# Index membership lists (late 2025 / early 2026 composition).
# ---------------------------------------------------------------------------

# DJIA 30 — post 2024 NVDA/SHW additions. NVDA replaced INTC (Nov 2024) and
# SHW replaced DOW Inc. (Nov 2024), so DOW Inc. is NOT in the current DJIA.
DJIA_30: List[str] = [
    "AAPL", "AMGN", "AMZN", "AXP", "BA", "CAT", "CRM", "CSCO", "CVX", "DIS",
    "GS",   "HD",   "HON",  "IBM", "JNJ", "JPM", "KO",  "MCD", "MMM", "MRK",
    "MSFT", "NKE",  "NVDA", "PG",  "SHW", "TRV", "UNH", "V",   "VZ",  "WMT",
]

# NASDAQ-100 — ~100 names. Reflects late-2025 reconstitution.
NASDAQ_100: List[str] = [
    "AAPL", "MSFT", "NVDA", "AMZN", "AVGO", "META", "GOOGL", "GOOG", "TSLA",
    "COST", "NFLX", "TMUS", "ASML", "AZN", "PEP", "CSCO", "ADBE", "LIN",
    "AMD",  "INTU", "QCOM", "TXN", "ISRG", "AMGN", "BKNG", "HON", "PANW",
    "CMCSA","AMAT", "ADP", "VRTX","SBUX", "GILD","ADI", "MU", "MELI", "LRCX",
    "REGN", "KLAC", "PDD", "MDLZ","INTC","CTAS","SNPS","CDNS","CRWD","MAR",
    "ORLY", "FTNT", "ABNB", "PYPL","CEG", "MRVL","NXPI","DASH","MNST","WDAY",
    "ADSK", "ROP",  "AEP",  "PCAR","CHTR","ROST","FAST","KDP", "PAYX","ODFL",
    "FANG", "DDOG", "EA",   "VRSK","KHC", "EXC", "CTSH","XEL", "CCEP","BKR",
    "LULU", "CSGP", "GEHC", "TTWO","DXCM","IDXX","ANSS","ZS",  "ON",  "TEAM",
    "CDW",  "WBD",  "MCHP", "GFS", "MDB", "ARM", "BIIB","SMCI","TTD", "ILMN",
    "WBA",  "LCID",  # both retained as legacy NDX members (WBA inactive)
]

# S&P 100 (OEX) — top ~100 names from S&P 500 by market cap. The OEX is a
# fixed list of ~100 large caps; the membership below reflects late 2025.
SP_100: List[str] = [
    "AAPL", "ABBV", "ABT", "ACN",  "ADBE", "AMD", "AMGN", "AMT", "AMZN",
    "AVGO", "AXP",  "BA",  "BAC",  "BK",   "BKNG","BLK","BMY",  "BRK.B","C",
    "CAT",  "CHTR", "CL",  "CMCSA","COF", "COP", "COST","CRM", "CSCO","CVS",
    "CVX",  "DE",   "DHR", "DIS",  "DUK",  "EMR", "F",   "GD",  "GE",
    "GILD", "GM",   "GOOG","GOOGL","GS",   "HD",  "HON", "IBM", "INTC","INTU",
    "ISRG", "JNJ",  "JPM", "KHC",  "KO",   "LIN", "LLY", "LMT", "LOW", "MA",
    "MCD",  "MDLZ", "MDT", "MET",  "META", "MMM", "MO",  "MRK", "MS",  "MSFT",
    "NEE",  "NFLX", "NKE", "NOW",  "NVDA", "ORCL","PEP", "PFE", "PG",  "PM",
    "PYPL", "QCOM", "RTX", "SBUX", "SCHW", "SO",  "SPG", "T",   "TGT", "TMO",
    "TMUS", "TSLA", "TXN", "UNH",  "UNP",  "UPS", "USB", "V",   "VZ",  "WFC",
    "WMT",  "XOM",
]


# ---------------------------------------------------------------------------
# Per-ticker metadata. Anything NOT explicitly tagged here defaults to
# maturity_stage="standard_compounder" and active=True.
# ---------------------------------------------------------------------------

# (ticker, name, exchange, sector, industry)
TickerMeta = Tuple[str, str, str, str, str]

METADATA: List[TickerMeta] = [
    # --- Mega-cap tech / comm services -----------------------------------
    ("AAPL",  "Apple Inc.",                              "NASDAQ", "Technology",             "Consumer Electronics"),
    ("MSFT",  "Microsoft Corporation",                   "NASDAQ", "Technology",             "Software"),
    ("NVDA",  "NVIDIA Corporation",                      "NASDAQ", "Technology",             "Semiconductors"),
    ("AMZN",  "Amazon.com, Inc.",                        "NASDAQ", "Consumer Discretionary", "E-Commerce"),
    ("GOOGL", "Alphabet Inc. Class A",                   "NASDAQ", "Communication Services", "Internet Services"),
    ("GOOG",  "Alphabet Inc. Class C",                   "NASDAQ", "Communication Services", "Internet Services"),
    ("META",  "Meta Platforms, Inc.",                    "NASDAQ", "Communication Services", "Social Media"),
    ("TSLA",  "Tesla, Inc.",                             "NASDAQ", "Consumer Discretionary", "Automobiles"),
    ("AVGO",  "Broadcom Inc.",                           "NASDAQ", "Technology",             "Semiconductors"),
    ("NFLX",  "Netflix, Inc.",                           "NASDAQ", "Communication Services", "Streaming Media"),
    ("ORCL",  "Oracle Corporation",                      "NYSE",   "Technology",             "Software"),
    ("ADBE",  "Adobe Inc.",                              "NASDAQ", "Technology",             "Software"),
    ("CRM",   "Salesforce, Inc.",                        "NYSE",   "Technology",             "Software"),
    ("AMD",   "Advanced Micro Devices, Inc.",            "NASDAQ", "Technology",             "Semiconductors"),
    ("INTC",  "Intel Corporation",                       "NASDAQ", "Technology",             "Semiconductors"),
    ("INTU",  "Intuit Inc.",                             "NASDAQ", "Technology",             "Software"),
    ("QCOM",  "QUALCOMM Incorporated",                   "NASDAQ", "Technology",             "Semiconductors"),
    ("TXN",   "Texas Instruments Incorporated",          "NASDAQ", "Technology",             "Semiconductors"),
    ("AMAT",  "Applied Materials, Inc.",                 "NASDAQ", "Technology",             "Semiconductor Equipment"),
    ("LRCX",  "Lam Research Corporation",                "NASDAQ", "Technology",             "Semiconductor Equipment"),
    ("KLAC",  "KLA Corporation",                         "NASDAQ", "Technology",             "Semiconductor Equipment"),
    ("MU",    "Micron Technology, Inc.",                 "NASDAQ", "Technology",             "Semiconductors"),
    ("ADI",   "Analog Devices, Inc.",                    "NASDAQ", "Technology",             "Semiconductors"),
    ("MRVL",  "Marvell Technology, Inc.",                "NASDAQ", "Technology",             "Semiconductors"),
    ("NXPI",  "NXP Semiconductors N.V.",                 "NASDAQ", "Technology",             "Semiconductors"),
    ("MCHP",  "Microchip Technology Incorporated",       "NASDAQ", "Technology",             "Semiconductors"),
    ("ON",    "ON Semiconductor Corporation",            "NASDAQ", "Technology",             "Semiconductors"),
    ("ASML",  "ASML Holding N.V.",                       "NASDAQ", "Technology",             "Semiconductor Equipment"),
    ("ARM",   "Arm Holdings plc",                        "NASDAQ", "Technology",             "Semiconductors"),
    ("GFS",   "GlobalFoundries Inc.",                    "NASDAQ", "Technology",             "Semiconductors"),
    ("SMCI",  "Super Micro Computer, Inc.",              "NASDAQ", "Technology",             "Computer Hardware"),
    ("CDNS",  "Cadence Design Systems, Inc.",            "NASDAQ", "Technology",             "EDA Software"),
    ("SNPS",  "Synopsys, Inc.",                          "NASDAQ", "Technology",             "EDA Software"),
    ("ANSS",  "ANSYS, Inc.",                             "NASDAQ", "Technology",             "Engineering Software"),
    ("ADSK",  "Autodesk, Inc.",                          "NASDAQ", "Technology",             "Software"),
    ("WDAY",  "Workday, Inc.",                           "NASDAQ", "Technology",             "Software"),
    ("CRWD",  "CrowdStrike Holdings, Inc.",              "NASDAQ", "Technology",             "Cybersecurity"),
    ("PANW",  "Palo Alto Networks, Inc.",                "NASDAQ", "Technology",             "Cybersecurity"),
    ("FTNT",  "Fortinet, Inc.",                          "NASDAQ", "Technology",             "Cybersecurity"),
    ("ZS",    "Zscaler, Inc.",                           "NASDAQ", "Technology",             "Cybersecurity"),
    ("DDOG",  "Datadog, Inc.",                           "NASDAQ", "Technology",             "Software"),
    ("MDB",   "MongoDB, Inc.",                           "NASDAQ", "Technology",             "Software"),
    ("TEAM",  "Atlassian Corporation",                   "NASDAQ", "Technology",             "Software"),
    ("CSCO",  "Cisco Systems, Inc.",                     "NASDAQ", "Technology",             "Networking Equipment"),
    ("IBM",   "International Business Machines",         "NYSE",   "Technology",             "IT Services"),
    ("ACN",   "Accenture plc",                           "NYSE",   "Technology",             "IT Services"),
    ("NOW",   "ServiceNow, Inc.",                        "NYSE",   "Technology",             "Software"),
    ("CTSH",  "Cognizant Technology Solutions",          "NASDAQ", "Technology",             "IT Services"),
    ("CDW",   "CDW Corporation",                         "NASDAQ", "Technology",             "IT Distribution"),
    ("PAYX",  "Paychex, Inc.",                           "NASDAQ", "Industrials",            "HR & Payroll Services"),
    ("ADP",   "Automatic Data Processing, Inc.",         "NASDAQ", "Industrials",            "HR & Payroll Services"),
    ("VRSK",  "Verisk Analytics, Inc.",                  "NASDAQ", "Industrials",            "Data Analytics"),
    ("CSGP",  "CoStar Group, Inc.",                      "NASDAQ", "Industrials",            "Data Analytics"),
    ("FAST",  "Fastenal Company",                        "NASDAQ", "Industrials",            "Industrial Distribution"),
    ("ODFL",  "Old Dominion Freight Line, Inc.",         "NASDAQ", "Industrials",            "Trucking"),
    ("PCAR",  "PACCAR Inc",                              "NASDAQ", "Industrials",            "Heavy Truck Manufacturing"),
    ("CTAS",  "Cintas Corporation",                      "NASDAQ", "Industrials",            "Uniform Services"),
    ("ROP",   "Roper Technologies, Inc.",                "NASDAQ", "Industrials",            "Diversified Industrials"),

    # --- Communication services / media ----------------------------------
    ("CMCSA", "Comcast Corporation",                     "NASDAQ", "Communication Services", "Media"),
    ("DIS",   "The Walt Disney Company",                 "NYSE",   "Communication Services", "Media & Entertainment"),
    ("CHTR",  "Charter Communications, Inc.",            "NASDAQ", "Communication Services", "Cable"),
    ("WBD",   "Warner Bros. Discovery, Inc.",            "NASDAQ", "Communication Services", "Media"),
    ("TMUS",  "T-Mobile US, Inc.",                       "NASDAQ", "Communication Services", "Telecom"),
    ("VZ",    "Verizon Communications Inc.",             "NYSE",   "Communication Services", "Telecom"),
    ("T",     "AT&T Inc.",                               "NYSE",   "Communication Services", "Telecom"),
    ("EA",    "Electronic Arts Inc.",                    "NASDAQ", "Communication Services", "Video Games"),
    ("TTWO",  "Take-Two Interactive Software, Inc.",     "NASDAQ", "Communication Services", "Video Games"),
    ("TTD",   "The Trade Desk, Inc.",                    "NASDAQ", "Communication Services", "Digital Advertising"),

    # --- Consumer discretionary ------------------------------------------
    ("HD",    "The Home Depot, Inc.",                    "NYSE",   "Consumer Discretionary", "Specialty Retail"),
    ("LOW",   "Lowe's Companies, Inc.",                  "NYSE",   "Consumer Discretionary", "Specialty Retail"),
    ("MCD",   "McDonald's Corporation",                  "NYSE",   "Consumer Discretionary", "Restaurants"),
    ("SBUX",  "Starbucks Corporation",                   "NASDAQ", "Consumer Discretionary", "Restaurants"),
    ("NKE",   "NIKE, Inc.",                              "NYSE",   "Consumer Discretionary", "Apparel"),
    ("BKNG",  "Booking Holdings Inc.",                   "NASDAQ", "Consumer Discretionary", "Online Travel"),
    ("ABNB",  "Airbnb, Inc.",                            "NASDAQ", "Consumer Discretionary", "Online Travel"),
    ("MAR",   "Marriott International, Inc.",            "NASDAQ", "Consumer Discretionary", "Hotels"),
    ("ORLY",  "O'Reilly Automotive, Inc.",               "NASDAQ", "Consumer Discretionary", "Auto Parts Retail"),
    ("ROST",  "Ross Stores, Inc.",                       "NASDAQ", "Consumer Discretionary", "Off-Price Retail"),
    ("TGT",   "Target Corporation",                      "NYSE",   "Consumer Discretionary", "Discount Retail"),
    ("LULU",  "Lululemon Athletica Inc.",                "NASDAQ", "Consumer Discretionary", "Apparel"),
    ("F",     "Ford Motor Company",                      "NYSE",   "Consumer Discretionary", "Automobiles"),
    ("GM",    "General Motors Company",                  "NYSE",   "Consumer Discretionary", "Automobiles"),
    ("LCID",  "Lucid Group, Inc.",                       "NASDAQ", "Consumer Discretionary", "Automobiles"),
    ("DASH",  "DoorDash, Inc.",                          "NASDAQ", "Consumer Discretionary", "Online Delivery"),
    ("MELI",  "MercadoLibre, Inc.",                      "NASDAQ", "Consumer Discretionary", "E-Commerce"),
    ("PDD",   "PDD Holdings Inc.",                       "NASDAQ", "Consumer Discretionary", "E-Commerce"),

    # --- Consumer staples -------------------------------------------------
    ("WMT",   "Walmart Inc.",                            "NYSE",   "Consumer Staples",       "Hypermarkets"),
    ("COST",  "Costco Wholesale Corporation",            "NASDAQ", "Consumer Staples",       "Hypermarkets"),
    ("PG",    "Procter & Gamble Company",                "NYSE",   "Consumer Staples",       "Household Products"),
    ("CL",    "Colgate-Palmolive Company",               "NYSE",   "Consumer Staples",       "Household Products"),
    ("KO",    "The Coca-Cola Company",                   "NYSE",   "Consumer Staples",       "Soft Drinks"),
    ("PEP",   "PepsiCo, Inc.",                           "NASDAQ", "Consumer Staples",       "Soft Drinks"),
    ("MDLZ",  "Mondelez International, Inc.",            "NASDAQ", "Consumer Staples",       "Packaged Foods"),
    ("KHC",   "The Kraft Heinz Company",                 "NASDAQ", "Consumer Staples",       "Packaged Foods"),
    ("MNST",  "Monster Beverage Corporation",            "NASDAQ", "Consumer Staples",       "Soft Drinks"),
    ("KDP",   "Keurig Dr Pepper Inc.",                   "NASDAQ", "Consumer Staples",       "Soft Drinks"),
    ("CCEP",  "Coca-Cola Europacific Partners plc",      "NASDAQ", "Consumer Staples",       "Soft Drinks"),
    ("MO",    "Altria Group, Inc.",                      "NYSE",   "Consumer Staples",       "Tobacco"),
    ("PM",    "Philip Morris International Inc.",        "NYSE",   "Consumer Staples",       "Tobacco"),
    ("WBA",   "Walgreens Boots Alliance, Inc.",          "NASDAQ", "Consumer Staples",       "Drug Retail"),
    ("CVS",   "CVS Health Corporation",                  "NYSE",   "Consumer Staples",       "Drug Retail"),

    # --- Health care ------------------------------------------------------
    ("LLY",   "Eli Lilly and Company",                   "NYSE",   "Health Care",            "Pharmaceuticals"),
    ("JNJ",   "Johnson & Johnson",                       "NYSE",   "Health Care",            "Pharmaceuticals"),
    ("MRK",   "Merck & Co., Inc.",                       "NYSE",   "Health Care",            "Pharmaceuticals"),
    ("ABBV",  "AbbVie Inc.",                             "NYSE",   "Health Care",            "Pharmaceuticals"),
    ("PFE",   "Pfizer Inc.",                             "NYSE",   "Health Care",            "Pharmaceuticals"),
    ("BMY",   "Bristol-Myers Squibb Company",            "NYSE",   "Health Care",            "Pharmaceuticals"),
    ("AZN",   "AstraZeneca PLC",                         "NASDAQ", "Health Care",            "Pharmaceuticals"),
    ("AMGN",  "Amgen Inc.",                              "NASDAQ", "Health Care",            "Biotechnology"),
    ("GILD",  "Gilead Sciences, Inc.",                   "NASDAQ", "Health Care",            "Biotechnology"),
    ("VRTX",  "Vertex Pharmaceuticals Inc.",             "NASDAQ", "Health Care",            "Biotechnology"),
    ("REGN",  "Regeneron Pharmaceuticals",               "NASDAQ", "Health Care",            "Biotechnology"),
    ("BIIB",  "Biogen Inc.",                             "NASDAQ", "Health Care",            "Biotechnology"),
    ("UNH",   "UnitedHealth Group Inc.",                 "NYSE",   "Health Care",            "Managed Care"),
    ("TMO",   "Thermo Fisher Scientific Inc.",           "NYSE",   "Health Care",            "Life Sciences Tools"),
    ("DHR",   "Danaher Corporation",                     "NYSE",   "Health Care",            "Life Sciences Tools"),
    ("ABT",   "Abbott Laboratories",                     "NYSE",   "Health Care",            "Medical Devices"),
    ("MDT",   "Medtronic plc",                           "NYSE",   "Health Care",            "Medical Devices"),
    ("ISRG",  "Intuitive Surgical, Inc.",                "NASDAQ", "Health Care",            "Medical Devices"),
    ("DXCM",  "DexCom, Inc.",                            "NASDAQ", "Health Care",            "Medical Devices"),
    ("IDXX",  "IDEXX Laboratories, Inc.",                "NASDAQ", "Health Care",            "Diagnostics"),
    ("ILMN",  "Illumina, Inc.",                          "NASDAQ", "Health Care",            "Life Sciences Tools"),
    ("GEHC",  "GE HealthCare Technologies Inc.",         "NASDAQ", "Health Care",            "Medical Devices"),

    # --- Financials -------------------------------------------------------
    ("BRK.B", "Berkshire Hathaway Inc.",                 "NYSE",   "Financials",             "Diversified"),
    ("JPM",   "JPMorgan Chase & Co.",                    "NYSE",   "Financials",             "Banks"),
    ("BAC",   "Bank of America Corporation",             "NYSE",   "Financials",             "Banks"),
    ("WFC",   "Wells Fargo & Company",                   "NYSE",   "Financials",             "Banks"),
    ("C",     "Citigroup Inc.",                          "NYSE",   "Financials",             "Banks"),
    ("USB",   "U.S. Bancorp",                            "NYSE",   "Financials",             "Banks"),
    ("BK",    "The Bank of New York Mellon Corporation", "NYSE",   "Financials",             "Banks"),
    ("MS",    "Morgan Stanley",                          "NYSE",   "Financials",             "Investment Banking"),
    ("GS",    "The Goldman Sachs Group, Inc.",           "NYSE",   "Financials",             "Investment Banking"),
    ("SCHW",  "The Charles Schwab Corporation",          "NYSE",   "Financials",             "Brokers"),
    ("BLK",   "BlackRock, Inc.",                         "NYSE",   "Financials",             "Asset Management"),
    ("V",     "Visa Inc.",                               "NYSE",   "Financials",             "Payments"),
    ("MA",    "Mastercard Incorporated",                 "NYSE",   "Financials",             "Payments"),
    ("AXP",   "American Express Company",                "NYSE",   "Financials",             "Payments"),
    ("PYPL",  "PayPal Holdings, Inc.",                   "NASDAQ", "Financials",             "Payments"),
    ("FI",    "Fiserv, Inc.",                            "NYSE",   "Financials",             "Payments"),
    ("SPGI",  "S&P Global Inc.",                         "NYSE",   "Financials",             "Financial Data"),
    ("CB",    "Chubb Limited",                           "NYSE",   "Financials",             "Insurance"),
    ("MMC",   "Marsh & McLennan Companies, Inc.",        "NYSE",   "Financials",             "Insurance Brokerage"),
    ("TRV",   "The Travelers Companies, Inc.",           "NYSE",   "Financials",             "Insurance"),
    ("MET",   "MetLife, Inc.",                           "NYSE",   "Financials",             "Insurance"),
    ("AIG",   "American International Group, Inc.",      "NYSE",   "Financials",             "Insurance"),
    ("COF",   "Capital One Financial Corporation",       "NYSE",   "Financials",             "Consumer Finance"),
    ("FANG",  "Diamondback Energy, Inc.",                "NASDAQ", "Energy",                 "Oil & Gas E&P"),

    # --- Industrials ------------------------------------------------------
    ("CAT",   "Caterpillar Inc.",                        "NYSE",   "Industrials",            "Heavy Machinery"),
    ("DE",    "Deere & Company",                         "NYSE",   "Industrials",            "Agricultural Machinery"),
    ("BA",    "The Boeing Company",                      "NYSE",   "Industrials",            "Aerospace & Defense"),
    ("RTX",   "RTX Corporation",                         "NYSE",   "Industrials",            "Aerospace & Defense"),
    ("LMT",   "Lockheed Martin Corporation",             "NYSE",   "Industrials",            "Aerospace & Defense"),
    ("GD",    "General Dynamics Corporation",            "NYSE",   "Industrials",            "Aerospace & Defense"),
    ("GE",    "GE Aerospace",                            "NYSE",   "Industrials",            "Aerospace & Defense"),
    ("HON",   "Honeywell International Inc.",            "NASDAQ", "Industrials",            "Diversified Industrials"),
    ("MMM",   "3M Company",                              "NYSE",   "Industrials",            "Diversified Industrials"),
    ("EMR",   "Emerson Electric Co.",                    "NYSE",   "Industrials",            "Diversified Industrials"),
    ("ETN",   "Eaton Corporation plc",                   "NYSE",   "Industrials",            "Electrical Equipment"),
    ("UNP",   "Union Pacific Corporation",               "NYSE",   "Industrials",            "Railroads"),
    ("UPS",   "United Parcel Service, Inc.",             "NYSE",   "Industrials",            "Logistics"),
    ("FDX",   "FedEx Corporation",                       "NYSE",   "Industrials",            "Logistics"),
    ("CPRT",  "Copart, Inc.",                            "NASDAQ", "Industrials",            "Auto Services"),

    # --- Energy -----------------------------------------------------------
    ("XOM",   "Exxon Mobil Corporation",                 "NYSE",   "Energy",                 "Integrated Oil & Gas"),
    ("CVX",   "Chevron Corporation",                     "NYSE",   "Energy",                 "Integrated Oil & Gas"),
    ("COP",   "ConocoPhillips",                          "NYSE",   "Energy",                 "Oil & Gas E&P"),
    ("BKR",   "Baker Hughes Company",                    "NASDAQ", "Energy",                 "Oil & Gas Services"),

    # --- Materials --------------------------------------------------------
    ("LIN",   "Linde plc",                               "NASDAQ", "Materials",              "Industrial Gases"),
    ("SHW",   "The Sherwin-Williams Company",            "NYSE",   "Materials",              "Specialty Chemicals"),

    # --- Utilities --------------------------------------------------------
    ("NEE",   "NextEra Energy, Inc.",                    "NYSE",   "Utilities",              "Electric Utilities"),
    ("DUK",   "Duke Energy Corporation",                 "NYSE",   "Utilities",              "Electric Utilities"),
    ("SO",    "The Southern Company",                    "NYSE",   "Utilities",              "Electric Utilities"),
    ("AEP",   "American Electric Power Company, Inc.",   "NASDAQ", "Utilities",              "Electric Utilities"),
    ("EXC",   "Exelon Corporation",                      "NASDAQ", "Utilities",              "Electric Utilities"),
    ("XEL",   "Xcel Energy Inc.",                        "NASDAQ", "Utilities",              "Electric Utilities"),
    ("CEG",   "Constellation Energy Corporation",        "NASDAQ", "Utilities",              "Electric Utilities"),

    # --- Real Estate ------------------------------------------------------
    ("AMT",   "American Tower Corporation",              "NYSE",   "Real Estate",            "Telecom REITs"),
    ("PLD",   "Prologis, Inc.",                          "NYSE",   "Real Estate",            "Industrial REITs"),
    ("SPG",   "Simon Property Group, Inc.",              "NYSE",   "Real Estate",            "Retail REITs"),
]


# Maturity-stage overrides preserved from the V1 universe.
MATURITY_OVERRIDES: Dict[str, str] = {
    "LCID": "pre_profit_growth",
    "INTC": "recovery_stabilization",
    "BA":   "recovery_stabilization",
    "GE":   "recovery_rerating",
    "WBA":  "recovery_stabilization",
}

INACTIVE: Dict[str, str] = {
    "WBA": (
        "Walgreens Boots Alliance taken private by Sycamore Partners "
        "(late 2025); no longer files with SEC."
    ),
}


def build_index_membership(ticker: str, djia: Set[str], ndx: Set[str], sp100: Set[str]) -> List[str]:
    """Build the ordered index_membership list for a ticker.

    Order: S&P 500, S&P 100, NASDAQ-100, DJIA. A ticker is in S&P 500 if it is
    in DJIA, S&P 100, or — for NDX names — domiciled in the US and otherwise
    eligible. A small group of NDX names are foreign-domiciled and NOT in the
    S&P 500: ASML, AZN, ARM, GFS, CCEP, MELI, PDD.
    """
    NDX_NOT_IN_SPX = {"ASML", "AZN", "ARM", "GFS", "CCEP", "MELI", "PDD"}

    out: List[str] = []
    in_djia  = ticker in djia
    in_ndx   = ticker in ndx
    in_sp100 = ticker in sp100

    in_spx = in_djia or in_sp100 or (in_ndx and ticker not in NDX_NOT_IN_SPX)
    if in_spx:
        out.append("S&P 500")
    if in_sp100:
        out.append("S&P 100")
    if in_ndx:
        out.append("NASDAQ-100")
    if in_djia:
        out.append("DJIA")
    return out


def build() -> dict:
    djia  = set(DJIA_30)
    ndx   = set(NASDAQ_100)
    sp100 = set(SP_100)

    # Sanity: every ticker referenced by an index list must have metadata.
    meta_by_ticker: Dict[str, TickerMeta] = {m[0]: m for m in METADATA}
    missing = sorted((djia | ndx | sp100) - set(meta_by_ticker))
    if missing:
        raise RuntimeError(f"index lists reference tickers without metadata: {missing}")

    # Universe = union of all index members, plus any extras that have metadata
    # (currently none — we keep the universe equal to the union).
    universe_tickers = sorted(djia | ndx | sp100)

    entries: List[dict] = []
    for tkr in universe_tickers:
        _, name, exchange, sector, industry = meta_by_ticker[tkr]
        entry: dict = {
            "ticker": tkr,
            "name": name,
            "exchange": exchange,
            "index_membership": build_index_membership(tkr, djia, ndx, sp100),
            "sector": sector,
            "industry": industry,
            "maturity_stage": MATURITY_OVERRIDES.get(tkr, "standard_compounder"),
            "active": tkr not in INACTIVE,
        }
        if tkr in INACTIVE:
            entry["inactive_reason"] = INACTIVE[tkr]
        entries.append(entry)

    return {
        "version": "2.0.0",
        "last_updated": str(date.today()),
        "description": (
            f"LTHCS V2 universe — full DJIA 30 + NASDAQ-100 + S&P 100, "
            f"deduped to {len(entries)} unique tickers. Includes pre-profit "
            f"(LCID) and recovery (INTC, BA, GE) test cases."
        ),
        "tickers": entries,
    }


def main() -> None:
    data = build()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print(f"wrote {len(data['tickers'])} tickers to {OUT_PATH}")


if __name__ == "__main__":
    main()
