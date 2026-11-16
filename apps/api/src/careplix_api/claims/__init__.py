"""Claims: follow submitted claims to payment (RCM_CLEARINGHOUSE_PLAN RC1 and RC6).

- :mod:`.models`: claims, each claim status answer kept as evidence, the payer directory.
- :mod:`.service`: one claim status check through the clearinghouse gateway.
- :mod:`.follow_up`: the re-check schedule by claim age, and the payer call when electronic status
  is stale or unavailable.
- :mod:`.samples`: synthetic claims and their synthetic 277 answers until the EHR billing pull.
"""

from careplix_api.claims import models  # noqa: F401  (registers the tables with create_all)
