from prd_agent.historical.retrieval import HistoricalPrdRetriever
from prd_agent.historical.tool import HistoricalPrdSearchTool
from prd_agent.sources.models import ReadAction

from .support import binding_for, write_fixture


def test_historical_tool_validates_versioned_contract_and_returns_candidates(tmp_path):
    store, corpus = write_fixture(tmp_path)
    action = ReadAction(
        tool_id="historical_prd_search",
        tool_schema_version="1",
        source_binding=binding_for(corpus),
        arguments={"query": "订单 completed", "limit": 1},
        purpose="查找历史状态命名",
    )

    result = HistoricalPrdSearchTool(HistoricalPrdRetriever(store)).execute(
        action,
        investigation_id="investigation-tool",
        owner_id="owner-1",
        project_id="project-1",
    )

    assert len(result.items) == 1
    assert result.items[0].document_id == "public-order"
