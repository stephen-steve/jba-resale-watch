import unittest

from rakuten_lowest import Coupon, Item, best_plan, calc_points, parse_coupon, rank


def item(price, included, shop="s", rate=1):
    return Item(name=f"{shop}-{price}", price=price, shop_code=shop, shop_name=shop,
                url=f"https://example/{shop}/{price}", postage_included=included,
                point_rate=rate)


class TestLowest(unittest.TestCase):
    def test_postage_included_has_no_shipping(self):
        p = best_plan(item(1000, True))
        self.assertEqual(p.qty, 1)
        self.assertEqual(p.shipping, 0)

    def test_over_threshold_is_free(self):
        p = best_plan(item(3980, False))
        self.assertEqual(p.shipping, 0)

    def test_cheap_single_with_shipping_switches_to_bulk(self):
        # 1000円・送料別 → 単品なら +800円。4個(4000円)で送料無料の方が安い
        p = best_plan(item(1000, False), shipping_fee=800)
        self.assertEqual(p.qty, 4)
        self.assertEqual(p.shipping, 0)

    def test_single_cheapest_but_shipping_loses(self):
        # 単品900円(送料別) vs 1100円(送料込み)
        plans = rank([item(900, False, "a"), item(1100, True, "b")], allow_bulk=False)
        self.assertEqual(plans[0].item.shop_code, "b")

    def test_bulk_still_cheaper(self):
        # まとめ買いOKなら 900円×5個=4500円 で送料無料 → 1個あたり 900円台
        plans = rank([item(900, False, "a"), item(1100, True, "b")])
        self.assertEqual(plans[0].item.shop_code, "a")
        self.assertEqual(plans[0].qty, 5)

    def test_coupon_and_points(self):
        coupons = dict([parse_coupon("a=500@3000")])
        p = best_plan(item(4400, True, "a", rate=10), coupons=coupons)
        self.assertEqual(p.coupon, 500)
        self.assertEqual(p.points, calc_points(3900, 10, True))  # 354
        self.assertEqual(p.total, 4400 - 500 - 354)

    def test_coupon_condition_not_met(self):
        self.assertEqual(Coupon(amount=500, min_total=3000).discount(2999), 0)
        self.assertEqual(Coupon(percent=10).discount(5000), 500)


if __name__ == "__main__":
    unittest.main()
