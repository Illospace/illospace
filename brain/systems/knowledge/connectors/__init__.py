"""Built-in knowledge source connectors."""

from brain.systems.knowledge.connectors.domain_records import DomainRecordsConnector
from brain.systems.knowledge.connectors.github import GitHubConnector
from brain.systems.knowledge.connectors.memory import MemoryConnector
from brain.systems.knowledge.connectors.slack import SlackKnowledgeConnector

__all__ = [
    "DomainRecordsConnector",
    "GitHubConnector",
    "MemoryConnector",
    "SlackKnowledgeConnector",
]
