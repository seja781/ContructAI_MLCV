from types import SimpleNamespace

from fastapi.testclient import TestClient

import app.backend.main as main


client = TestClient(main.app)


def _estimate() -> dict:
    return {
        "locality": "Baner",
        "price_lakh": 93.9,
        "low_lakh": 76.4,
        "high_lakh": 123.7,
        "rate_per_sqft": 8940,
        "price_period": "Oct 2026",
        "range_meaning": "About 80 in 100 similar flats are priced inside this range",
        "notes": [],
        "model_accuracy": {"test_mae_lakh": 16.65},
    }


def test_house_price_prediction_uses_trained_model_inputs(monkeypatch) -> None:
    received = {}

    def predict(**kwargs):
        received.update(kwargs)
        return _estimate()

    predictor = SimpleNamespace(predict=predict, PredictionError=ValueError)
    monkeypatch.setattr(main, "_house_price_predictor", lambda: predictor)

    response = client.post(
        "/house-price/predict",
        json={
            "locality": "Baner",
            "bedrooms": 2,
            "area_sqft": 1050,
            "floor_number": 5,
            "total_floors": 12,
        },
    )

    assert response.status_code == 200
    assert response.json()["price_lakh"] == 93.9
    assert response.json()["low_lakh"] == 76.4
    assert received == {
        "locality": "Baner",
        "bedrooms": 2,
        "area_sqft": 1050.0,
        "floor_number": 5,
        "total_floors": 12,
    }


def test_house_price_prediction_rejects_out_of_range_area_before_model_call(monkeypatch) -> None:
    def unavailable_predictor():
        raise AssertionError("Invalid input must not call the model")

    monkeypatch.setattr(main, "_house_price_predictor", unavailable_predictor)

    response = client.post(
        "/house-price/predict",
        json={
            "locality": "Baner",
            "bedrooms": 2,
            "area_sqft": 100,
            "floor_number": 5,
        },
    )

    assert response.status_code == 422


def test_house_price_localities_returns_model_localities(monkeypatch) -> None:
    predictor = SimpleNamespace(localities=lambda search: ["Baner"] if search == "ban" else ["Baner", "Kothrud"])
    monkeypatch.setattr(main, "_house_price_predictor", lambda: predictor)

    response = client.get("/house-price/localities", params={"search": "ban"})

    assert response.status_code == 200
    assert response.json() == ["Baner"]


def test_house_price_prediction_maps_unknown_locality_to_validation_error(monkeypatch) -> None:
    class FakePredictionError(ValueError):
        pass

    def predict(**kwargs):
        assert kwargs["locality"] == "Atlantis"
        raise FakePredictionError("Unknown locality 'Atlantis'.")

    predictor = SimpleNamespace(predict=predict, PredictionError=FakePredictionError)
    monkeypatch.setattr(main, "_house_price_predictor", lambda: predictor)

    response = client.post(
        "/house-price/predict",
        json={
            "locality": "Atlantis",
            "bedrooms": 2,
            "area_sqft": 1050,
            "floor_number": 5,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Unknown locality 'Atlantis'."
