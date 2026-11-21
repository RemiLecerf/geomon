from geomon.ingest import rpg


def test_archive_of_a_region_and_year():
    names = ["RPG_1-0__SHP_LAMB93_R28_2008-01-01", "RPG_1-0__SHP_LAMB93_R28-2011_2011-01-01",
             "RPG_1-0__SHP_LAMB93_R84_2008-01-01", "RPG_1-0__SHP_LAMB93_R11_2008-01-01",
             "RPG_1-0__SHP_LAMB93_FXX_2017-04-07"]
    assert rpg.package_name(names, "28", 2008) == "RPG_1-0__SHP_LAMB93_R28_2008-01-01"
    assert rpg.package_name(names, "28", 2011) == "RPG_1-0__SHP_LAMB93_R28-2011_2011-01-01"
    assert rpg.package_name(names, "84", 2008) == "RPG_1-0__SHP_LAMB93_R84_2008-01-01"
    assert rpg.package_name(names, "8", 2008) is None            # R84 is not region 8
    assert rpg.package_name(names, "28", 2009) is None


def test_group_codes_match_the_parcels():
    assert rpg._group_code("01") == "1" and rpg._group_code(18) == "18" and rpg._group_code(None) is None


def test_ilot_years_stop_before_the_parcels():
    years = rpg.ilot_years()
    assert years[0] == rpg.settings.rpg_ilot_first_year and years[-1] == min(2014, rpg.settings.rpg_first_year - 1)
