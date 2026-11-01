# Rules and deterministic checks

Rules GAME --fetch retrieves the official game page and rules iframe. It extracts scoring tables and collapsed sections without executing scripts. Output includes retrieval time, source fingerprints, section/table counts, completeness, and missing sections. Extraction completeness does not establish that a rule model is verified.

If capture fails, inspect the exact page in the integrated browser. Expand unreadable sections, follow the displayed iframe, and identify remaining gaps. Do not invent conditions or treat an unavailable premium injury feed as a clean health report.

For authorized local evidence storage:

    & $holdetPython -X utf8 $holdetCli rules '<game>' --fetch --save

Read the complete normalized text and relevant tables before writing a profile. A shared filename or sport match is insufficient season evidence. Include conditions for participation, scoring exclusions, clean sheets, fees, contracts, captain eligibility, and deadlines. Separate construction rules from trading windows and retention exceptions.

Register only rules actually reviewed:

    & $holdetPython -X utf8 $holdetCli rules '<game>' --audit '<profile.json>' --review-note '<checked sections and unresolved conditions>'

Audit uses the latest saved capture by default; --capture accepts explicit raw capture JSON. Fetch with --raw includes original HTML. Audit requires complete matching evidence and a review note. It binds locale/slug, game page, document URL, fingerprint, and access date; it does not infer rule values automatically. A newer saved capture that changes the document invalidates the previous audit for certification. Captures and audits are immutable history.

No season profiles are bundled. Each game needs its own locally reviewed evidence; missing rules remain unverified. Repository tests use invented profiles and HTML only.

## Profile fields

Start from the profile object printed by rules GAME. Set supported fields only where established:

- sport: soccer, cycling, formula1, golf, or nfl.
- salary_cap, budget_enabled, roster_size, max_from_team.
- position_limits: lists of [normalized_position, minimum, maximum]; or category_count and category_size.
- base_contracts, gold_contracts, gold_unlimited. Null counts alone do not establish unlimited changes.
- free_through_round is the contract exemption; fee_free_through_round is the separate fee exemption.
- transfer_fee_basis_points and bank_interest_basis_points: 100 means 1%. Rounding is floor, ceil, nearest, or null when unknown. Unknown fractional fee rounding prevents certification of an affected trade.
- captain_count, captain_multiplier (2 for normal growth plus one extra bonus), captain_positive_only, captain_positions (e.g. ["driver"]).
- allow_club_excess_without_trades for documented retention exceptions. New lineups still obey the club limit.
- initial_roster_fee_basis_points when a new team is created after the fee-free window. A zero general transfer fee also establishes a zero initial fee; otherwise an unknown initial fee leaves funding unverified.

Audit fills provenance from the capture. A review note cannot establish an unsupported field. Leave unknown values null and disclose validation limits.

## Interpreting validation

The validator checks stable IDs, current prices and bank, final roster, eligible captain positions, formation/categories, club limits, duplicates, inactive purchases, fees, contracts, and personal restrictions. It never submits anything.

Keep official legality, data certainty, and forecasting confidence separate. Missing rules and round/price mismatches remain unverified. Injury flags produce warnings; disabled new purchases are rejected. Existing deactivated players may be retained or sold.

New lineups use the verified initial budget and documented initial fees. Unknown fees leave funding unverified. Unavailable historical prices are not reconstructed.

For existing teams, compare no change with proposed transfers. Personal max_transfers is a remaining allowance from now, separate from official contracts. Future prices/fees are conditional; today's prices cannot guarantee a multi-round plan.
