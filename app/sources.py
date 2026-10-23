"""
app/sources.py
Where a batch of Telegram messages came from. Built by the listener, passed
through the worker's bridge, and read by the dispatcher.
"""
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SubchannelLink:
    """A channel just joined from a root-feed post with a single invite link.
    The dispatcher links it to the company the post is about — the company
    name the LLM extracts from the post, else the channel title."""

    chat_id: int
    title: str
    invite_token: str
    environment: str  # MAIN | TEST, from the root feed the post came from
    main_feed_msg_id: int


@dataclass(frozen=True)
class MessageSource:
    """chat_id: the chat the messages were posted in (their ledger key).
    context: dispatcher label — "main_feed" | "test_feed" | "subchannel".
    company_id: the batch's company when it is known for certain (a reply to,
      or a re-post of, a company's invite post); authoritative for every action.
    subchannel: set for an invite post that joined a channel not linked yet."""

    chat_id: int
    context: str
    company_id: Optional[int] = None
    subchannel: Optional[SubchannelLink] = None
