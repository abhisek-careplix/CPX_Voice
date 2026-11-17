"""Forms by secure link: the pre-visit questionnaire and intake/registration.

A link is a random token (only its keyed hash is stored), tied to one case, with an expiry. The
patient's answers are sealed at rest (PhiBox). Submitting wakes the case's playbook.
"""
