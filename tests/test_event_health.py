from mcp_second_brain.event_health import DeliveryHealth
from mcp_second_brain.query_event_store import SinkStats


def test_delivery_health_reports_transitions_without_repeated_alerts():
    health = DeliveryHealth()
    def stats(loss, written=20, pending=0):
        return SinkStats(written+loss, written, loss, 0, 0, 0, pending)
    assert health.observe(stats(0)) is None
    assert health.observe(stats(1)) == 'query_event_delivery_degraded'
    assert health.observe(stats(2)) is None
    assert health.observe(stats(2, written=21)) is None
    assert health.observe(stats(2, written=21)) is None
    assert health.observe(stats(2, written=21)) == 'query_event_delivery_recovered'
    assert health.observe(stats(2)) is None
    assert health.observe(stats(3)) == 'query_event_delivery_degraded'
