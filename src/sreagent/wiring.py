"""Builds the real provider, tools and PR opener from Config. Shared by the CLI and the server."""

import posixpath

import github_tools
import k8s_tools
import prometheus_tools
from agent import Agent
from config import Config
from tools import ToolRegistry


def _github(config: Config) -> github_tools.GitHubReader:
    return github_tools.GitHubReader(config.github_repo, config.github_token,
                                     config.github_branch, config.values_file)


def build_registry(config: Config, extra_tools=()) -> ToolRegistry:
    prom = prometheus_tools.PrometheusClient(config.prometheus_url)
    kube = k8s_tools.KubeReader(config.app_namespace, context=config.kube_context)
    tools = (prometheus_tools.make_tools(prom) + k8s_tools.make_tools(kube)
             + github_tools.make_tools(_github(config)) + list(extra_tools))
    return ToolRegistry(tools, config.tool_output_max_chars)


def build_agent(config: Config, extra_tools=()) -> Agent:
    from anthropic_provider import AnthropicProvider  # only needed when calling the model
    provider = AnthropicProvider(config.model, config.max_tokens, config.model_timeout_seconds)
    return Agent(provider, build_registry(config, extra_tools), config.max_tool_calls,
                 config.investigation_timeout_seconds)


def build_pr_opener(config: Config):
    import pr_tool  # needs ruamel.yaml
    return pr_tool.PullRequestOpener(
        _github(config), config.values_file,
        posixpath.join(posixpath.dirname(config.values_file), "values.yaml"))
