import pytest
from battery_insight.ingest import validate_source
from test_ingest import source


@pytest.mark.parametrize("reserved", ["source_path", "source_hash", "source_row", "cell_id", "test_id", "segment_id"])
def test_config_cannot_replace_trusted_provenance(reserved):
    spec = source()
    spec["columns"][reserved] = {"column": "fake"}
    with pytest.raises(ValueError, match="reserved"):
        validate_source(spec)
