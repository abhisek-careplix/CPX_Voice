"""Prior authorization, end to end (CarePlix Practice).

- :mod:`.playbook`: the ``prior_authorization`` playbook's trigger and steps (detect, packet,
  submit, track, write back, tell the patient), registered into the playbook engine.
- :mod:`.packet`: the packet checklist from the case facts and the EHR mirror.
- :mod:`.rules`: payer rules (which services need auth with which payer).
- :mod:`.api`: the Studio's work list, packet and rules endpoints.

Options for electronic 278 submission are compared in ``docs/solution/PRIOR_AUTH.md``.
"""

from careplix_api.priorauth import models  # noqa: F401  (registers the tables with create_all)
