"""Which institution's install a recorded URL means.

A capability records absolute URLs, because that is what it navigated to. An allowlist is
a *pattern*, precisely so one artifact is valid at every institution running the same
vendor product. Compose those and a capability recorded at riverside, replayed from
lakeside's deployment, navigates to riverside, passes the allowlist, and reports success
about the wrong credit union's member. Rebasing means the deployment decides which
install a run acts on.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def rebase(recorded: str, entry_url: str) -> tuple[str, str | None]:
    """Point a recorded URL at this deployment's install.

    Returns the URL to navigate to, and a note naming what changed when anything
    did — the note goes onto the step result, because an operator reading evidence
    should never have to work out why the URL in the log differs from the URL in
    the artifact.

    Left alone: a relative URL (no origin to replace), a URL whose origin already
    matches, and anything at all when the deployment declares no entry URL.
    """
    if not entry_url:
        return recorded, None

    target = urlsplit(recorded)
    if not target.scheme or not target.netloc:
        return recorded, None

    entry = urlsplit(entry_url)
    if (target.scheme, target.netloc) == (entry.scheme, entry.netloc):
        return recorded, None

    # The entry URL may carry a path prefix — an app mounted at /corebank rather
    # than at the root — and dropping it would produce a 404 that looks like drift.
    prefix = entry.path.rstrip("/")
    rebased = urlunsplit(
        (entry.scheme, entry.netloc, prefix + target.path, target.query, target.fragment)
    )
    return rebased, (
        f"rebased onto this deployment: recorded at {target.scheme}://{target.netloc}, "
        f"navigating to {entry.scheme}://{entry.netloc}"
    )
