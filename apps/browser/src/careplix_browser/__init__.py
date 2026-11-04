"""CarePlix browser runtime: isolated Chromium sessions for the supervised browser agent.

This service only drives browsers. It holds no customer records and makes no decisions: the API
(``careplix_api.browser``) decides every action, asks a person to approve it, and then tells this
service to perform it. See docs/solution/BROWSER_AGENT_PLAN.md.
"""
