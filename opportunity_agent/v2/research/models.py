"""Research role configuration, shared credentials but independently selected models."""
from ...config import _setting
from ...llm_client import LLMClient


def role_client(role: str, timeout: float):
    prefix = f'RESEARCH_{role.upper()}'
    client = LLMClient(model=_setting(prefix + '_MODEL', 'RESEARCH_FAST_MODEL'),
                       timeout_seconds=timeout, retries=0)
    effort = _setting(prefix + '_REASONING_EFFORT')
    if effort in {'none', 'minimal', 'low', 'medium', 'high'}:
        client.reasoning_effort = effort
    client.tls_compatibility_retry = False
    return client
