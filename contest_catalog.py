"""Shared Hydro contest pagination for the daemon and benchmark scanner."""

DEFAULT_START_PAGE = 2


def positive_int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError('页码必须是正整数') from None
    if isinstance(value, bool) or number < 1 or (isinstance(value, float) and number != value):
        raise ValueError('页码必须是正整数')
    return number


def list_contests(session, root: str, domain: str, max_pages: int = 20,
                  *, start_page: int = DEFAULT_START_PAGE) -> list:
    """Fetch at most max_pages pages, starting at start_page (including that page)."""
    start_page, max_pages = positive_int(start_page), positive_int(max_pages)
    contests, seen = [], set()
    for page in range(start_page, start_page + max_pages):
        response = session.get(f"{root.rstrip('/')}/d/{domain}/contest",
                               headers={'Accept': 'application/json'},
                               params={'page': page, 'limit': 50}, timeout=30)
        if response.status_code != 200:
            break
        documents = response.json().get('tdocs', [])
        if not documents:
            break
        added = 0
        for document in documents:
            cid = document.get('_id')
            if cid and cid in seen:
                continue
            if cid:
                seen.add(cid)
            contests.append(document)
            added += 1
        # Stop on a short page or on servers that keep returning the same page.
        if len(documents) < 50 or not added:
            break
    return contests
