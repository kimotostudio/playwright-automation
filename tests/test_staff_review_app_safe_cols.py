import pandas as pd

from src.staff_review_app import _confidence, ensure_col


def test_ensure_col_returns_series_for_missing_column() -> None:
    df = pd.DataFrame({"id": ["1", "2"]})
    series = ensure_col(df, "confidence_level", "")
    assert isinstance(series, pd.Series)
    assert series.tolist() == ["", ""]
    assert list(series.index) == list(df.index)


def test_confidence_compute_does_not_crash_when_column_missing() -> None:
    df = pd.DataFrame(
        {
            "id": ["1", "2"],
            "status": ["prepared_full", "prepared_review_needed"],
        }
    )
    df["confidence"] = [
        _confidence(s, c)
        for s, c in zip(
            ensure_col(df, "status", "").astype(str).tolist(),
            ensure_col(df, "confidence_level", "").astype(str).tolist(),
        )
    ]
    assert df["confidence"].tolist() == ["high", "low"]
