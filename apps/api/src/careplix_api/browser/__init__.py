"""Browser engine (BROWSER_AGENT_PLAN, PRODUCT_ARCHITECTURE §2): supervised browser runs.

A person names a task and a site and ticks "Under human supervision". The agent (Gemini or Claude
computer use) proposes a plan, then one action at a time; every action is classified by risk and,
where the rules say so, shown to the supervisor as an approval card before the runtime performs it.
"""
