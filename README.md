Problem:

1) Take a goal in natural language
2) Use LLM to accomplish that goal
    Goal-driven agent loop

    Input: goal + target app/URL/entry point
    observe --> decide --> act against live surface until goal met or (max steps, timeout, dead-end)    


3) record the successful run as structured, reusable artifact
    - typed, versioned description of the flow
    - steps take, how each target element / control is identified, any data to extract
    - use stable element/control targeting
    - report success/failure


4) Replay that artifact determinisitically
    - re-run without LLM
    Output

    distinguish:
     - expected business outcomes ("no such member")
     - recoverable conditions ("dismiss known interstitial, wait/retry)
     - hard failures

    - success (with outputs)
    - a known business outcome
    - failure with enough detail to debug

5) Escalate to human when stuck
    - when system can't safely proceed, route to human operator

    Detect and route
        Intervention request / carry context, current state, why it stopped
    Take control of live session
        - same live session, then hand control back, preserve context
    Pause / cede control / resume33

6) Stay within safety guardrails
    - avoid leaking / persisting sensitive data
    - allowlist (permitted domains/routes, which action types allowed)
    - safe/reversible actions, risky/irreversible ones (block / require confirmation / flag)


Design for heterogenity & scale
- Surface abstraction: url vs legacy web app vs desktop app?
- multi-tenant reuse across multiple users?

/evidence/
mock UI: for customized 


Example Tasks:
    - "look up member 12345 and read their current savings balance"
    - "open a new sub-account for this member and reach the confirmation screen"
    - "add a specific item to the cart and reach the checkout review page"

Some examples I have:
    Read Tasks:
        - Get current balance of account X
        - get last N transactions of account X
        - find transaction matching amount Y or from Z merchant in account X
    Write tasks:
        - Transfer funds from X to Y.
        - Pay a bill
        - Request a loan

From the assignment
    - "record not found"
    - permission denials
    - unexpected confirmation dialogs
    - session/timeout expiry
    - transient slowness
    - outright app errors

Some examples I have:
    - account not logged in / timed out
    - need to press confirm / go
    - need to scroll
    - correct MM-DD-YYYY date
    - missing transactions / wrong date
    - account info not found for X or Y
    - network / server error
    - need to check transaction happened, and correct amount


Key decisions for this project

1) Computer Use Mechanism
 - Perception: computer vision / Vision LLM
    - since DOMs might not work for desktop / legacy websites
    - pure vision maybe more reliable and robust
 - OS: dockerized computer with x11vnc
    - since desktop apps need to be handled and human operator may need to take over securely
    - playwright service inside if it's a demo app

