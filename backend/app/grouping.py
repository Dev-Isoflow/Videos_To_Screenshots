"""Suggested groups for a session's screens (e.g. "Onboarding", "Cart").

The groups themselves come from the same Claude request that labels each
screen (see labeling.py). This module only tidies the answer, because the
model can be sloppy: it may invent filenames, list a screen twice, skip one,
or return groups out of order. The plugin expects every screen in at most one
group and the groups in flow order, so that's enforced here rather than trusted.
"""

from typing import Iterable, List

# Used for screens the model didn't assign to any group.
LEFTOVER_NAME = "Other screens"
ALL_NAME = "All screens"


def normalise_groups(suggested: Iterable, filenames: List[str], method: str = "ai") -> List[dict]:
    """Returns [{"name", "method", "filenames"}] covering every filename once.

    `suggested` is any iterable of objects with `.name` and `.filenames`.
    `filenames` is every extracted frame, in flow order.
    """
    position = {name: i for i, name in enumerate(filenames)}
    claimed: set = set()
    groups: List[dict] = []

    for group in suggested:
        members = []
        for filename in group.filenames:
            if filename in position and filename not in claimed:
                claimed.add(filename)
                members.append(filename)
        if not members:
            continue
        members.sort(key=position.__getitem__)
        groups.append({"name": group.name.strip() or "Untitled group", "method": method, "filenames": members})

    # Groups follow the flow, whatever order the model listed them in.
    groups.sort(key=lambda g: position[g["filenames"][0]])

    leftovers = [name for name in filenames if name not in claimed]
    if leftovers:
        name = LEFTOVER_NAME if groups else ALL_NAME
        groups.append({"name": name, "method": method, "filenames": leftovers})

    return groups


def restrict_to(groups: List[dict], keep: set) -> List[dict]:
    """Drops screens that aren't in `keep` (e.g. frames excluded during review)
    and any group that ends up empty."""
    result = []
    for group in groups:
        names = [name for name in group["filenames"] if name in keep]
        if names:
            result.append({**group, "filenames": names})
    return result
