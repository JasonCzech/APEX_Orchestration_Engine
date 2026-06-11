from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from apex.graphs.pipeline.graph import builder, graph


def test_toy_graph_invoke() -> None:
    result = graph.invoke({"request": "demo"})
    assert "demo" in result["plan"]
    assert result["summary"].startswith("Completed.")


def test_toy_graph_checkpoints_per_node() -> None:
    checkpointed = builder.compile(checkpointer=InMemorySaver())
    config: RunnableConfig = {"configurable": {"thread_id": "t-1"}}
    checkpointed.invoke({"request": "demo"}, config)
    history = list(checkpointed.get_state_history(config))
    # input + plan + report checkpoints at minimum
    assert len(history) >= 3
    assert history[0].values["summary"].startswith("Completed.")
