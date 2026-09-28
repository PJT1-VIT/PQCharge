"""Rule 1 must refuse the receive-loop task, and ONLY that task."""

import asyncio

from csms.dispatch import _called_from_receive_loop


class _Station:
    def __init__(self) -> None:
        self.handling_message = False
        self.receive_task = None


def test_idle_station_allows_dispatch():
    assert _called_from_receive_loop(_Station()) is False


def test_receive_loop_task_is_refused():
    async def scenario():
        cp = _Station()
        cp.handling_message = True
        cp.receive_task = asyncio.current_task()
        return _called_from_receive_loop(cp)

    assert asyncio.run(scenario()) is True


def test_other_task_is_allowed_while_station_is_busy():
    async def scenario():
        cp = _Station()
        cp.handling_message = True
        cp.receive_task = asyncio.current_task()

        async def orchestrator():
            return _called_from_receive_loop(cp)

        return await asyncio.ensure_future(orchestrator())

    assert asyncio.run(scenario()) is False


def test_unrecorded_task_falls_back_to_refusing():
    cp = _Station()
    cp.handling_message = True
    assert _called_from_receive_loop(cp) is True
