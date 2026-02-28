import os
import tempfile
import unittest
from datetime import date, timedelta

import flight_discountinator as fd


class FlightDiscountinatorTests(unittest.TestCase):
    def make_candidate(
        self,
        destination: str,
        price: float,
        source: str = "amadeus_inspiration",
        days_out: int = 45,
    ) -> fd.FareCandidate:
        return fd.FareCandidate(
            source=source,
            origin="CMH",
            destination=destination,
            departure_date=(date.today() + timedelta(days=days_out)).isoformat(),
            return_date=(date.today() + timedelta(days=days_out + 4)).isoformat(),
            currency="USD",
            total_price=price,
        )

    def test_rank_prefers_cheaper_destination(self) -> None:
        candidates = [
            self.make_candidate("MIA", 180.0, "amadeus_inspiration"),
            self.make_candidate("SEA", 260.0, "amadeus_inspiration"),
            self.make_candidate("MIA", 175.0, "amadeus_offers"),
            self.make_candidate("SEA", 255.0, "amadeus_offers"),
        ]
        ranked = fd.rank_destinations(candidates, top_n=2)
        self.assertEqual(ranked[0].destination, "MIA")
        self.assertLess(ranked[0].best_price, ranked[1].best_price)

    def test_market_discount_uses_destination_best_fares(self) -> None:
        candidates = [
            self.make_candidate("NYC", 100.0, "amadeus_inspiration"),
            self.make_candidate("NYC", 400.0, "amadeus_inspiration"),
            self.make_candidate("LAX", 200.0, "amadeus_inspiration"),
        ]
        ranked = fd.rank_destinations(candidates, top_n=2)
        nyc = next(item for item in ranked if item.destination == "NYC")
        self.assertAlmostEqual(nyc.market_discount_pct, 33.33, delta=0.05)

    def test_consistency_uses_source_best_prices(self) -> None:
        candidates = [
            self.make_candidate("CHI", 100.0, "amadeus_inspiration"),
            self.make_candidate("CHI", 300.0, "amadeus_inspiration"),
            self.make_candidate("CHI", 110.0, "amadeus_offers"),
            self.make_candidate("SFO", 230.0, "amadeus_inspiration"),
        ]
        ranked = fd.rank_destinations(candidates, top_n=2)
        chi = next(item for item in ranked if item.destination == "CHI")
        self.assertGreater(chi.consistency_pct, 85.0)

    def test_option_depth_bonus_caps(self) -> None:
        candidates = [
            self.make_candidate("BOS", 200.0, "amadeus_inspiration"),
            self.make_candidate("BOS", 199.0, "amadeus_inspiration"),
            self.make_candidate("BOS", 198.0, "amadeus_inspiration"),
            self.make_candidate("BOS", 197.0, "amadeus_inspiration"),
            self.make_candidate("BOS", 196.0, "amadeus_offers"),
        ]
        ranked = fd.rank_destinations(candidates, top_n=1)
        self.assertEqual(ranked[0].option_depth_pct, 100.0)
        self.assertEqual(ranked[0].sample_count, 5)

    def test_time_window_bonus_boundaries(self) -> None:
        self.assertEqual(
            fd.time_window_bonus((date.today() + timedelta(days=10)).isoformat()),
            1.0,
        )
        self.assertEqual(
            fd.time_window_bonus((date.today() + timedelta(days=60)).isoformat()),
            0.75,
        )
        self.assertEqual(
            fd.time_window_bonus((date.today() + timedelta(days=150)).isoformat()),
            0.6,
        )
        self.assertEqual(
            fd.time_window_bonus((date.today() + timedelta(days=240)).isoformat()),
            0.4,
        )
        self.assertEqual(
            fd.time_window_bonus((date.today() - timedelta(days=1)).isoformat()),
            0.25,
        )
        self.assertEqual(fd.time_window_bonus("not-a-date"), 0.5)

    def test_parser_accepts_new_audit_flags(self) -> None:
        parser = fd.build_parser()
        args = parser.parse_args(
            [
                "--probe-workers",
                "6",
                "--max-retries",
                "5",
                "--retry-backoff",
                "0.25",
            ]
        )
        self.assertEqual(args.probe_workers, 6)
        self.assertEqual(args.max_retries, 5)
        self.assertAlmostEqual(args.retry_backoff, 0.25)

    def test_load_dotenv_sets_missing_env_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = os.path.join(tmp_dir, ".env")
            with open(env_path, "w", encoding="utf-8") as handle:
                handle.write("DOTENV_NEW=value_one\n")
                handle.write("DOTENV_EXISTS=from_file\n")

            previous_exists = os.environ.get("DOTENV_EXISTS")
            previous_new = os.environ.get("DOTENV_NEW")
            try:
                os.environ["DOTENV_EXISTS"] = "keep_existing"
                os.environ.pop("DOTENV_NEW", None)

                fd.load_dotenv(env_path)

                self.assertEqual(os.environ["DOTENV_NEW"], "value_one")
                self.assertEqual(os.environ["DOTENV_EXISTS"], "keep_existing")
            finally:
                if previous_exists is None:
                    os.environ.pop("DOTENV_EXISTS", None)
                else:
                    os.environ["DOTENV_EXISTS"] = previous_exists

                if previous_new is None:
                    os.environ.pop("DOTENV_NEW", None)
                else:
                    os.environ["DOTENV_NEW"] = previous_new

    def test_normalize_origin_validation(self) -> None:
        self.assertEqual(fd.normalize_origin(" cmh "), "CMH")
        with self.assertRaises(ValueError):
            fd.normalize_origin("CM")
        with self.assertRaises(ValueError):
            fd.normalize_origin("12A")

    def test_resolve_date_window_defaults(self) -> None:
        start, end = fd.resolve_date_window(None, None, 120)
        self.assertGreaterEqual((start - date.today()).days, 14)
        self.assertEqual((end - start).days, 120)


if __name__ == "__main__":
    unittest.main()
