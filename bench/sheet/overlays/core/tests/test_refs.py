from sheet.refs import col_name, col_num, parse_ref, ref_name


def test_columns_round_trip():
    for n in (1, 26, 27, 52, 703):
        assert col_num(col_name(n)) == n


def test_parse_ref_any_case():
    assert parse_ref("b12") == (2, 12)
    assert parse_ref("AA1") == (27, 1)


def test_not_a_ref():
    assert parse_ref("A0") is None
    assert parse_ref("11") is None


def test_ref_name():
    assert ref_name(28, 3) == "AB3"
