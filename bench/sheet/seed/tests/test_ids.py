from sheet.ids import new_id


def test_ids_are_unique_within_a_workbook():
    ids = [new_id() for _ in range(30)]
    assert len(set(ids)) == len(ids)
