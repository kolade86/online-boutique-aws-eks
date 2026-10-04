"""Builds the real provider and tools from Config. Shared by the CLI and (later) the server."""

import github_tools
import k8s_tools
import prometheus_tools
from agent import Agent
from config import Config
from tools import ToolRegistry


def build_registry(config: Config) -> ToolRegistry:
    prom = prometheus_tools.PrometheusClient(config.prometheus_url)
    kube = k8s_tools.KubeReader(config.app_namespace, context=config.kube_context)
    gh = github_tools.GitHubReader(config.github_repo, config.github_token,
                                   config.github_branch, config.values_file)
    tools = (prometheus_tools.make_tools(prom) + k8s_tools.make_tools(kube)
             + github_tools.make_tools(gh))
    return ToolRegistry(tools, config.tool_output_max_chars)


def build_agent(config: Config) -> Agent:
    from anthropic_provider import AnthropicProvider  # only needed when calling the model
    provider = AnthropicProvider(config.model, config.max_tokens, config.model_timeout_seconds)
    return Agent(provider, build_registry(config), config.max_tool_calls,
                 config.investigation_timeout_seconds)
