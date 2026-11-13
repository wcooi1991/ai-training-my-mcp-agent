# Solar ROI: Assumptions and Rates

## Rates per kWh

| Item | Rate | Source / confidence |
|---|---|---|
| Export credit (ATAP) | RM 0.2703 | PathGreen quote (page 2), for consumption under 1,500 kWh/month. Some web guides cite ~RM 0.218, so confirm against the first ATAP bill. |
| Import cost avoided (self-consumed solar) | RM 0.4443 | Assumption: generation 27.03 + capacity 4.55 + network 12.85 sen. Components from web guides, not the user's own bill. Matches what PathGreen's RM 236/month figure implied. |

Not included in the import rate: fixed monthly retail charge (unchanged by solar), fuel/ICPT adjustments, KWTBB levy, service tax. If any apply, the true avoided cost per kWh is slightly higher and savings are slightly conservative.

## Cost side

| Item | Amount |
|---|---|
| Solar package (negotiated) | RM 16,500 |
| EV cable and termination | RM 1,000 |
| Gross cost (no battery) | RM 17,500 |
| SuRIA Home rebate (RM 600/kWac, capped at RM 3,000) | -RM 3,000 |
| Net investment | RM 14,500 |

- The rebate is paid by TNB after commissioning, so the full amount is paid up front. Eligibility is assumed.
- The EV cable earns no savings but is counted in the investment (conservative). Excluding it, net cost is RM 13,500.

## Energy and savings assumptions

- Source data: logged October production, import and export (30 of 31 days, 7 Oct missing), averaged per day and scaled x30 to a month.
- Self-consumed kWh = production - export.
- Monthly savings = self-consumed kWh x 0.4443 + exported kWh x 0.2703.
- October result: 725 kWh produced, 376 kWh exported, 349 kWh self-consumed. Savings = RM 155 + RM 102 = ~RM 257/month.
- Payback = RM 14,500 / RM 257 = ~4.7 years.
- Export credit is used in full: October export credit (~RM 102) is below import cost (~RM 191), so none is wasted. ATAP credits do not carry forward.

## Not included in the payback figure

- Season: October treated as a typical month. The Nov-Feb monsoon may produce less.
- Tariffs: import and export rates held fixed, with no tariff rises, inflation or discounting.
- Panel degradation: excluded from the headline. Adds ~1 month to payback, lowers lifetime savings by ~RM 4,000 over 25 years.
- Inverter replacement: not included. 10-year warranty, so replacement of several thousand ringgit is likely in the 10-15 year range.
- Maintenance and financing: O&M free for 5 years, 0% payment plan assumed (no interest cost).
- ToU tariff: not assumed.
- Usage habits: consumption pattern assumed to stay as it is now.

## Sensitivity

| Scenario | Monthly savings | Payback |
|---|---|---|
| Base (0.4443 import / 0.2703 export) | RM 257 | 4.7 years |
| Export credit RM 0.218 | RM 237 | 5.1 years |
| Import avoided RM 0.40 | RM 241 | 5.0 years |
| Import avoided RM 0.50 | RM 276 | 4.4 years |

Next step: replace both rates with real figures from a recent TNB bill (after ATAP starts) and rerun.