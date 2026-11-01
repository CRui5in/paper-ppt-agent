"""Agent-mode diagnostics observe initial artifacts without another session."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.orchestrator import agent_pipeline


class AgentInitialDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_layout_findings_do_not_start_another_agent_session(self):
        with TemporaryDirectory() as directory:
            with patch.object(agent_pipeline, 'get_svg_files', return_value=[]), \
                    patch.object(agent_pipeline, '_agent_generation_contract_violations', return_value=['Text overlap']), \
                    patch.object(agent_pipeline, 'PersistentAgentSession') as session:
                events = [event async for event in agent_pipeline._report_agent_initial_contracts(Path(directory))]
                session.assert_not_called()
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].data['phase'], 'initial_generation')


if __name__ == '__main__':
    unittest.main()
