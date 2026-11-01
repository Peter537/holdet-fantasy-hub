# Strategy and proposal JSON

Strategies are stored separately from rules by locale, game slug, and fantasy-team ID. Save only when requested:

    & $holdetPython -X utf8 $holdetCli strategy '<game>' --team '<ID>' --set '<strategy.json>'

Example strategy:

    {
      "transfer_policy": "limited",
      "captain_policy": "fixed",
      "team_type": "basis",
      "max_transfers": 2,
      "reserve_contracts": 1,
      "start_round": 8,
      "end_round": 17,
      "identity": "Reliable minutes and long-term value",
      "fixed_captain_id": 123,
      "required_player_ids": [],
      "excluded_player_ids": []
    }

Transfer policy is active, limited, emergency_only, fixed, or unspecified. Captain policy is independently fixed, rotate, or unspecified. Limited needs max_transfers, a personal remaining budget for the horizon. Reserve_contracts preserves official contracts after a trade. Optional budget limits roster value; player lists express requested inclusions/exclusions. Team_type must reflect the actual Basis/Gold team, never upgrade its allowances.

For existing teams, a saved fixed captain ID takes precedence; otherwise the captured captain is the baseline. A new fixed team needs an explicit fixed captain ID.

Proposal format:

    {
      "schema_version": 1,
      "game": {"locale": "da", "slug": "example-game"},
      "target_round": 9,
      "teams": [
        {
          "team_id": 123456,
          "player_ids": [101, 102],
          "captain_ids": [101],
          "strategy": {"transfer_policy": "limited", "max_transfers": 1}
        }
      ]
    }

The abbreviated list illustrates the schema; final proposals need every selected player. Player_ids is the complete desired final lineup. Team_id identifies an existing cached team; omit it and provide a distinct name for a new initial team. Inline strategy values override saved settings for this proposal only.

Emergency-only changes need a substantive emergency_reason on the proposed team. It cannot override a completely fixed policy. Multiple teams yield exposure and pairwise overlap. Optional top-level max_shared_players enforces a requested hard limit; omission permits overlap.

Use validate GAME --proposal FILE, or pipe JSON into validate GAME --proposal - without writing a file. Do not rewrite saved policies to make proposals pass.
