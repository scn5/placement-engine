"""
app/telegram/join_policy.py
Which Telegram links the ingestion worker may follow, and which chats it may
join or read as feeds (MASTER_SPEC §2 "Join Scope"). Pure functions — no client
and no I/O — so every rule is unit-tested without touching Telegram.

Links are taken only from root-feed posts, never from subchannels:
  • private invites  t.me/+<hash>, t.me/joinchat/<hash>, tg://join?invite=<hash>
  • public channels  t.me/<username>, t.me/s/<username>, <username>.t.me,
                     tg://resolve?domain=<username>
  telegram.me and telegram.dog are accepted as aliases of t.me.
Never followed: message permalinks (t.me/<name>/<id>, t.me/c/…), phone-number
links (t.me/+<digits>), links with a query string (bot ?start=…, Mini Apps,
livestreams) and folder, sticker, theme, proxy, login and share links.

A link that passes is only a candidate: the chat it leads to must also pass
check_invite / check_chat — a channel or supergroup, never a person, a bot, a
basic group, a root feed, or an invite that needs admin approval.
"""
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional
from urllib.parse import parse_qs

from telethon import utils
from telethon.tl import types

PRIVATE = "private"
PUBLIC = "public"

# Why a resolved chat or an invite is refused.
PERSON_OR_BOT = "person_or_bot"
NOT_A_CHANNEL = "not_a_channel"
ROOT_FEED = "root_feed"
NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class InviteLink:
    """A followable link: an invite hash (PRIVATE, case-sensitive) or a public
    username (PUBLIC, lower-cased)."""

    kind: str
    token: str

    def masked(self) -> str:
        """For logs: an invite hash lets anyone join, so only its start is shown."""
        if self.kind == PRIVATE:
            return f"t.me/+{self.token[:4]}…"
        return f"t.me/{self.token}"


_HOST = r"(?:www\.)?(?:t\.me|telegram\.me|telegram\.dog)"
# A link ends at whitespace or at Markdown/HTML delimiters ("[label](url)", "<url>").
_BODY = r"[^\s()<>\[\]{}\"'`|\\]*"
_LINK_START = r"(?<![\w.@/-])"
_PATH_LINK_RE = re.compile(rf"{_LINK_START}(?:https?://)?{_HOST}/({_BODY})", re.IGNORECASE)
_SUBDOMAIN_LINK_RE = re.compile(
    rf"{_LINK_START}(?:https?://)?([A-Za-z][A-Za-z0-9_]{{3,31}})\.t\.me(?![\w.-])({_BODY})",
    re.IGNORECASE,
)
_TG_LINK_RE = re.compile(rf"{_LINK_START}tg://(join|resolve)\?({_BODY})", re.IGNORECASE)

_HASH_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
# Prose punctuation and Markdown emphasis that may follow a link.
_TRAILING = ".,;:!?…*~»”’"
# First path segments that are Telegram features, not usernames.
_RESERVED = frozenset({
    "joinchat", "addlist", "addstickers", "addemoji", "addtheme", "share", "login",
    "setlanguage", "contact", "proxy", "socks", "boost", "giftcode", "invoice",
    "confirmphone", "premium", "premium_offer", "premium_multigift", "stars",
    "stars_topup", "nft", "web", "apps",
})


def _public(name: str) -> Optional[InviteLink]:
    if not _USERNAME_RE.fullmatch(name) or name.lower() in _RESERVED:
        return None
    return InviteLink(PUBLIC, name.lower())


def _from_path(path: str) -> Optional[InviteLink]:
    """Classify the part of a t.me link after the host."""
    path = path.rstrip(_TRAILING)
    if "?" in path or "#" in path:
        return None  # bot deep links (?start=), Mini Apps, livestreams, …
    segments = path.split("/")
    if len(segments) > 1 and segments[-1] == "":
        segments.pop()  # "t.me/name/"
    head, rest = segments[0], segments[1:]
    if head.startswith("+"):
        token = head[1:]
        if rest or not _HASH_RE.fullmatch(token) or token.isdigit():
            return None  # t.me/+<digits> is a phone number
        return InviteLink(PRIVATE, token)
    if head.lower() == "joinchat":
        if len(rest) == 1 and _HASH_RE.fullmatch(rest[0]):
            return InviteLink(PRIVATE, rest[0])
        return None
    if head.lower() == "s" and len(rest) == 1:  # t.me/s/<username>: a channel's web preview
        head, rest = rest[0], []
    if rest:
        return None  # permalinks (t.me/<name>/<id>, t.me/c/<id>/<id>), app and topic links
    return _public(head)


def _from_tg(action: str, query: str) -> Optional[InviteLink]:
    params = parse_qs(query.rstrip(_TRAILING), keep_blank_values=True)
    if action.lower() == "join":
        values = params.get("invite", [])
        if set(params) == {"invite"} and len(values) == 1 and _HASH_RE.fullmatch(values[0]):
            return InviteLink(PRIVATE, values[0])
        return None
    values = params.get("domain", [])
    if set(params) == {"domain"} and len(values) == 1:  # any other key: a post, a bot start, a phone…
        return _public(values[0])
    return None


def find_invite_links(text: Optional[str]) -> List[InviteLink]:
    """Followable links in a root-feed post, in order of appearance, without duplicates."""
    if not text:
        return []
    found = []
    for match in _PATH_LINK_RE.finditer(text):
        found.append((match.start(), _from_path(match.group(1))))
    for match in _SUBDOMAIN_LINK_RE.finditer(text):
        name, body = match.groups()
        found.append((match.start(), _public(name) if body.rstrip(_TRAILING) in ("", "/") else None))
    for match in _TG_LINK_RE.finditer(text):
        found.append((match.start(), _from_tg(*match.groups())))

    links: List[InviteLink] = []
    for _, link in sorted(found, key=lambda item: item[0]):
        if link is not None and link not in links:
            links.append(link)
    return links


def is_channel_id(chat_id) -> bool:
    """True for a marked channel ID (-100…), i.e. a broadcast channel or a
    supergroup. A positive ID is a person or bot; -<n> is a basic group."""
    try:
        return utils.resolve_id(int(chat_id))[1] is types.PeerChannel
    except (TypeError, ValueError):
        return False


def is_broadcast_channel(chat) -> bool:
    """Root feeds must be broadcast channels: only their admins can post, so
    nobody else can make the worker join a chat."""
    return isinstance(chat, types.Channel) and bool(chat.broadcast)


def check_chat(chat, root_feed_ids: Iterable[int] = ()) -> Optional[str]:
    """Why a resolved chat must not be joined or linked as a subchannel, or None."""
    if isinstance(chat, types.User):
        return PERSON_OR_BOT
    if not isinstance(chat, types.Channel):
        return NOT_A_CHANNEL  # a basic group, or a chat this account was removed from
    if utils.get_peer_id(chat) in set(root_feed_ids):
        return ROOT_FEED
    return None


def check_invite(invite, root_feed_ids: Iterable[int] = ()) -> Optional[str]:
    """Why an invite preview (messages.checkChatInvite) must not be joined, or None."""
    if isinstance(invite, (types.ChatInviteAlready, types.ChatInvitePeek)):
        return check_chat(invite.chat, root_feed_ids)
    if isinstance(invite, types.ChatInvite):
        if not invite.channel:
            return NOT_A_CHANNEL  # an invite to a basic group
        if invite.request_needed:
            return NEEDS_APPROVAL  # joining would send the admins a join request
        return None
    return NOT_A_CHANNEL
