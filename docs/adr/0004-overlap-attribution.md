# Exchanges attribute to Steps by interval overlap, not assignment

An Exchange is owned by the Step that was active when it started, and every later Step it overlaps is recorded as an Overlap. It is not assigned to exactly one Step.

The obvious model — each Exchange belongs to one Step — was rejected because it destroys the most diagnostic fact available. Given a tap that fires a 900 ms request, followed by an assertion that fails on timeout, single-owner attribution reports either "the tap made a request" or "the assert made a request". Neither says *the assertion started while the request it depended on was still in flight*, which is the sentence that explains the failure.

## Consequences

Timeline output is interval-shaped rather than a flat list, which costs more tokens per Timeline and needs a more careful summary representation — hence `verbosity` on `get_timeline` and summary-first rendering by default. Exchanges belonging to no Step are reported as Background rather than being attributed or dropped.
