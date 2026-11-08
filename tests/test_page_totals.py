"""Build plan A6: reading the totals each report page showed (the shadow baseline). Layouts copied from the live
read_page output (2026-11-07); every number here is made up."""

from mseo.browser import extract


def sc_totals(clicks="21", impressions="1,050", ctr="2%", position="8.4"):
    lines = ["URL: https://search.google.com/search-console/performance/search-analytics", "main [ref_1]"]
    for label, value in [("Total clicks", clicks), ("Total impressions", impressions),
                                         ("Average CTR", ctr), ("Average position", position)]:
        lines += [" button [ref_a]", f'  generic "{label}" [ref_b]', f'   generic "{label}" [ref_c]',
                  f'  generic "{value}" [ref_d]', ' dialog [ref_e]', '  generic "is how many times" [ref_f]',
                  f'   generic "{label}" [ref_g]', '   link "Learn more" [ref_h] href="https://support.example/"']
    return "\n".join(lines)


def test_search_console_cards():
    assert extract.sc_page_totals(sc_totals()) == {"clicks": 21, "impressions": 1050, "ctr": 0.02, "position": 8.4}


def test_search_console_label_in_a_tooltip_never_takes_the_next_card_s_value():
    got = extract.sc_page_totals(sc_totals(clicks="—"))
    assert "clicks" not in got and got["impressions"] == 1050


def _header(names, named=True):
    out = ["row [ref_1]"]
    for n in names:
        out += [f' columnheader "{n}" [ref_h]' if named else " columnheader [ref_h]", "  button [ref_b]",
                f'   generic "{n}" [ref_g]']
    return out


def ga4_totals():
    head = _header(["Index"], named=False) + [' columnheader "Session primary channel group" [ref_x]',
                                              '  generic "Session primary channel group" [ref_y]',
                                              ' columnheader "Date" [ref_z]', '  generic "Date" [ref_z2]']
    head += _header(["Sessions", "Engaged sessions", "Engagement rate", "Key events", "Total revenue"], named=False)[1:]
    total = ["row [ref_9]", " columnheader [ref_a]", '  generic "Empty Summary Totals cell for Index column" [ref_b]',
             " columnheader [ref_c]", '  generic "Total" [ref_d]',
             " columnheader [ref_e]", '  generic "57" [ref_f]', '  generic "100% of total" [ref_g]',
             " columnheader [ref_h]", '  generic "100% of total" [ref_i]',
             " columnheader [ref_j]", '  generic "40.1%" [ref_k]',
             " columnheader [ref_l]", '  generic "3.00" [ref_m]', '  generic "100% of total" [ref_n]',
             " columnheader [ref_o]", '  generic "Empty Summary Totals cell for Total revenue column" [ref_p]',
             '  generic "$0.00" [ref_q]']
    return "\n".join(["URL: https://analytics.google.com/x", *head, "", *total])


def test_ga4_totals_row_is_read_from_the_right_and_skips_cells_without_a_number():
    assert extract.ga4_page_totals(ga4_totals()) == {"sessions": 57, "keyEvents": 3.0}


def ads_totals():
    names = ["Campaign state", "Campaign", "Campaign type", "Conversions", "Clicks", "Impr.", "Cost", "Phone calls"]
    total = ["row [ref_9]", " gridcell [ref_a]", '  generic "Total" [ref_b]', " gridcell [ref_c]", " gridcell [ref_d]",
             " gridcell [ref_e]", '  generic "4.50" [ref_f]', " gridcell [ref_g]", " gridcell [ref_h]",
             '  generic "1,204" [ref_i]', " gridcell [ref_j]", '  generic "$1,033.10" [ref_k]', " gridcell [ref_l]"]
    return "\n".join(["URL: https://ads.google.com/aw/reporteditor/view", *_header(names), "", *total])


def test_ads_total_row_maps_by_column_name():
    assert extract.ads_page_totals(ads_totals()) == {"conversions": 4.5, "impressions": 1204, "cost": 1033.1}


def test_unreadable_pages_give_nothing():
    for reader in extract.PAGE_TOTALS.values():
        assert reader("URL: x\nnothing here") == {}


def test_ga4_falls_back_to_the_page_text_for_numbers_read_page_leaves_out():
    text = ga4_totals().replace('  generic "57" [ref_f]\n', "") + "\n".join([
        "", "--- page text ---", "Title: Traffic acquisition", "Total", "Paid Social", "Index",
        "Empty Summary Totals cell for Index column", "Total", "57", "100% of total", "", "38", "100% of total", "",
        "40.1%", "Avg 0%", "", "3.00", "100% of total", "Empty Summary Totals cell for Total revenue column", "$0.00"])
    assert extract.ga4_page_totals(text) == {"sessions": 57, "engagedSessions": 38, "keyEvents": 3.0}
