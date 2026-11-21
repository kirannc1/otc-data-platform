"""Local IDs and expected crosswalks for the deterministic first seed."""
CUSTOMERS = [
    ("Metro Industrial Services", "Metro Ind. Services", "DEMO-100", "DEMO-100", "automatic"),
    ("Pioneer Engineering", "Pioneer Engineering", "DEMO-200", "DEMO-200", "automatic"),
    ("Apex Industrial Supply", "Apex Industrial", "DEMO-300", None, "review"),
    ("Summit Process Systems", "Summit Process", "DEMO-400", None, "review"),
    ("Harbor Utilities", "Valley Fabrication", "DEMO-500", "DEMO-600", "distinct"),
]
PRODUCTS = [
    ("PUMP", "PUMP", "Replacement pump", "ACME", "RP100", "Each", "Each", "automatic"),
    ("VALVE", "VALVE", "Valve", "ACME", "V50", "Each", "Each", "automatic"),
    ("FILT", "FLT", "Filter", "ACME", "F200", "Each", "Each", "review"),
    ("SEAL", "SEAL", "Seal", "ACME", "S20", "Each", "Pack", "review"),
    ("GAUGE", "HOSE", "Pressure gauge", "ACME", "PG300", "Each", "Each", "distinct"),
]
