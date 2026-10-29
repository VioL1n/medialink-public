import pytest
from medialink.curation import classify
from medialink.naming import build_plan, episode_info, file_id, library_folder, media_snapshot

MB, GB = 1024 ** 2, 1024 ** 3
MOVIE = {"id": 334541, "type": "movie", "title": "海边的曼彻斯特", "chinese_title": "海边的曼彻斯特",
         "original_title": "Manchester by the Sea", "year": 2016}
TV = {"id": 99, "type": "tv", "title": "中头奖还是要上班", "chinese_title": "中头奖还是要上班",
      "original_title": "The Ordinary Jackpot", "year": 2024, "seasons": {"1": 10}}
# Real release layouts observed in this library, including the ad videos that previously reached Emby.
MANCHESTER = "【高清影视之家发布 www.WHATMV.com】海边的曼彻斯特[简繁英字幕].Manchester.by.the.Sea.2016.EUR.1080p.BluRay.Remux.AVC.DTS-HD.MA.5.1-QuickIO"
JACKPOT = "【高清剧集网发布 www.PTHDTV.com】中头奖还是要上班[全10集][中文字幕].The.Ordinary.Jackpot.S01.1080p.DSNP.WEB-DL.AAC.2.0.H.264-BlackTV"
AD = "【更多无水印高品质资源请访问 www.Butailing.com】【更多无水印高品质资源请访问 www.Butailing.com】"


def manchester():
    return [{"path": f"{MANCHESTER}/【更多无水印高清电影请访问 www.BBQDDQ.com】【更多无水印高清电影请访问 www.BBQDDQ.com】.MKV", "size": 8 * MB},
            {"path": f"{MANCHESTER}/【更多无水印蓝光原盘请访问 www.BBQDDQ.com】【更多无水印蓝光原盘请访问 www.BBQDDQ.com】.MP4", "size": 12 * MB},
            {"path": f"{MANCHESTER}/Manchester.by.the.Sea.2016.EUR.1080p.BluRay.Remux.AVC.DTS-HD.MA.5.1-QuickIO.mkv", "size": 32 * GB}]


def jackpot(episodes=10):
    files = [{"path": f"{JACKPOT}/The.Ordinary.Jackpot.S01E{e:02d}.Episode.{e}.1080p.DSNP.WEB-DL.AAC.2.0.H.264-BlackTV.mkv",
              "size": 1300 * MB} for e in range(1, episodes + 1)]
    return files + [{"path": f"{JACKPOT}/{AD}.MP4", "size": 6 * MB}, {"path": f"{JACKPOT}/{AD}.MKV", "size": 9 * MB},
                    {"path": f"{JACKPOT}/更多资源.url", "size": 1}, {"path": f"{JACKPOT}/最新地址.txt", "size": 1}]


def library(files, media, **extra):
    return build_plan({"version": 2, "media": media, "files": files, **extra})


def test_ads_and_unrelated_files_are_excluded_but_episodes_kept():
    result = classify(jackpot())
    roles = {path.rsplit("/", 1)[1]: value["role"] for path, value in result.items()}
    assert roles[f"{AD}.MP4"] == roles[f"{AD}.MKV"] == "ad"
    assert roles["更多资源.url"] == roles["最新地址.txt"] == "other"
    assert sum(value["keep"] for value in result.values()) == 10


@pytest.mark.parametrize("files,kept", [
    # A site tag on the real film is kept because of its size, with a warning.
    ([{"path": "【www.example.com】谍影重重.2002.2160p.mkv", "size": 40 * GB},
      {"path": "Sample/sample.mkv", "size": 90 * MB}], {"【www.example.com】谍影重重.2002.2160p.mkv"}),
    ([{"path": f"[VCB-Studio] Show [{e:02d}][Ma10p_1080p][x265_flac].mkv", "size": GB} for e in (1, 2)]
     + [{"path": "SPs/[VCB-Studio] Show [NCOP][Ma10p_1080p].mkv", "size": 150 * MB},
        {"path": "[VCB-Studio] Show [Menu01][Ma10p_1080p].mkv", "size": 20 * MB},
        {"path": "Scans/cover.jpg", "size": MB}],
     {"[VCB-Studio] Show [01][Ma10p_1080p][x265_flac].mkv", "[VCB-Studio] Show [02][Ma10p_1080p][x265_flac].mkv"}),
    # Uniformly small old episodes are not mistaken for samples.
    ([{"path": f"Old.Show.S01E{e:02d}.mkv", "size": 25 * MB} for e in (1, 2, 3)],
     {"Old.Show.S01E01.mkv", "Old.Show.S01E02.mkv", "Old.Show.S01E03.mkv"}),
    ([{"path": "Movie.2019.1080p.mkv", "size": 8 * GB}, {"path": "Movie.2019.1080p-trailer.mkv", "size": 200 * MB},
      {"path": "Movie.2019.1080p.zh.srt", "size": 50_000}], {"Movie.2019.1080p.mkv", "Movie.2019.1080p.zh.srt"}),
])
def test_curation_keeps_only_feature_videos_and_subtitles(files, kept):
    assert {path for path, value in classify(files).items() if value["keep"]} == kept


def test_movie_ads_no_longer_collide_and_land_in_title_folder():
    result = library(manchester(), MOVIE)
    assert not result["errors"] and result["folder"] == "海边的曼彻斯特 (2016) [tmdbid=334541]"
    assert [item["destination"] for item in result["plan"]] == [
        "海边的曼彻斯特 (2016) [tmdbid=334541]/Manchester by the Sea (2016) - 1080P.mkv"]
    assert result["plan"][0]["target"] == f"{MANCHESTER}/Manchester by the Sea (2016) - 1080P.mkv"
    assert {item["role"] for item in result["excluded"]} == {"ad"}


def test_tv_pack_uses_season_folder_and_pairs_subtitles_by_episode():
    files = jackpot(2) + [{"path": f"{JACKPOT}/Subs/The.Ordinary.Jackpot.S01E02.chs.ass", "size": 40_000},
                          {"path": f"{JACKPOT}/Subs/readme.srt", "size": 100}]
    result = library(files, TV)
    assert not result["errors"]
    assert sorted(item["destination"] for item in result["plan"]) == [
        "中头奖还是要上班 (2024) [tmdbid=99]/Season 01/中头奖还是要上班 S01E01.mkv",
        "中头奖还是要上班 (2024) [tmdbid=99]/Season 01/中头奖还是要上班 S01E02.chs.ass",
        "中头奖还是要上班 (2024) [tmdbid=99]/Season 01/中头奖还是要上班 S01E02.mkv"]
    reasons = {item["source"].rsplit("/", 1)[1]: item["reason"] for item in result["excluded"]}
    assert "无法对应" in reasons["readme.srt"] and len(reasons) == 5


def test_library_folder_and_specials():
    assert library_folder({**MOVIE, "year": None}) == "海边的曼彻斯特 [tmdbid=334541]"
    assert library_folder({**MOVIE, "chinese_title": None}) == "Manchester by the Sea (2016) [tmdbid=334541]"
    result = library([{"path": "Specials/Show.S00E01.mkv", "size": GB}], {**TV, "seasons": {"0": 2, "1": 10}})
    assert result["plan"][0]["destination"] == "中头奖还是要上班 (2024) [tmdbid=99]/Season 00/中头奖还是要上班 S00E01.mkv"
    movie = library([{"path": "movie.1080p.mkv", "size": GB}], {**MOVIE, "year": None})
    assert movie["plan"][0]["destination"].endswith("/Manchester by the Sea - 1080P.mkv")


@pytest.mark.parametrize("path,selected,expected", [
    ("[KTXP][Grand_Blue_Dreaming_S3][05][GB_CN][AV1_opus][1080p].mkv", None, (3, 5)),
    ("[KTXP][Grand_Blue_Dreaming_S3][02v2][GB_CN][AV1_opus][1080p].mkv", None, (3, 2)),
    ("[LoliHouse] Yani Neko - 11 [WebRip 1080p HEVC-10bit AAC SRTx2].mkv", 1, (1, 11)),
    ("[Trix] Grand Blue Dreaming S03E01 [WEBRip 1080p AV1] (Multi Subs).mkv", None, (3, 1)),
    ("Show [12][1080p][2018].mkv", 1, (1, 12)),
    ("The.Show.Episode.7.1080p.mkv", 1, (1, 7)),
    ("[64bitsub][Nukitashi][11][BDRIP_1920x1080][AVC_2×FLAC].mkv", 1, (1, 11)),
    ("[64bitsub][Nukitashi][OVA_03][BDRIP_1920x1080][AVC_FLAC].mkv", 1, (0, 3)),
    ("[Group] Show - 05 [1920x1080].mkv", 1, (1, 5)), ("Show.SP2.1080p.mkv", None, (0, 2)),
    ("Movie [1080p].mkv", 1, (1, None)), ("Show [2018].mkv", 1, (1, None)),
    ("Show [ABCD1234].mkv", 1, (1, None)), ("Show [01][02].mkv", 1, (1, None)),
])
def test_release_group_episode_markers(path, selected, expected):
    assert episode_info(path, selected)[:2] == expected


def test_single_season_default_and_tmdb_validation():
    neko = {**TV, "seasons": {"0": 3, "1": 12}}
    result = library([{"path": "[LoliHouse] Yani Neko - 11 [WebRip 1080p].mkv", "size": GB}], neko)
    assert result["plan"][0]["destination"].endswith("Season 01/中头奖还是要上班 S01E11.mkv")
    assert "TMDB 唯一正片季" in result["plan"][0]["basis"]
    two = {**TV, "seasons": {"1": 12, "2": 12}}
    absolute = library([{"path": "Season 2/[Group] Show - 13 [1080p].mkv", "size": GB}], two)
    assert absolute["plan"][0]["episode_number"] == 1 and "绝对集号" in absolute["plan"][0]["basis"]
    explicit = library([{"path": "Show.S02E13.mkv", "size": GB}], two)
    assert "只有 12 集" in explicit["errors"][0]
    assert "没有第 3 季" in library([{"path": "Show.S03E01.mkv", "size": GB}], two)["errors"][0]
    trusted = library([{"path": "Show.S02E13.mkv", "size": GB}], two,
                      overrides={file_id("Show.S02E13.mkv"): {"episode_number": 13}})
    assert not trusted["errors"]
    # Snapshots taken before seasons were stored skip validation entirely.
    assert not library([{"path": "Show.S02E13.mkv", "size": GB}], {**TV, "seasons": None})["errors"]


def test_explicit_choices_override_curation():
    files = manchester()
    ad = files[0]["path"]
    included = library(files, MOVIE, inclusions={file_id(ad): True})
    assert any(item["source"] == ad for item in included["plan"])
    main = files[2]["path"]
    excluded = library(files, MOVIE, inclusions={file_id(main): False})
    assert "没有可入库" in excluded["errors"][0]
    selected = library(jackpot(2), TV, selected_paths=[jackpot(2)[0]["path"]])
    assert len(selected["plan"]) == 1
    assert any(item["reason"] == "下载前未勾选" for item in selected["excluded"])


def test_duplicate_versions_pause_instead_of_overwriting():
    files = [{"path": "A/Show.S01E01.1080p.mkv", "size": GB}, {"path": "B/Show.S01E01.1080p.mkv", "size": GB}]
    assert "同一个规范名称" in library(files, TV)["errors"][0]


def test_media_snapshot_keeps_episode_counts_for_tv_only():
    detail = {**TV, "seasons": [{"number": 0, "episodes": 2}, {"number": 1, "episodes": 10}]}
    assert media_snapshot(detail)["seasons"] == {"0": 2, "1": 10}
    assert "seasons" not in media_snapshot({**MOVIE, "seasons": [{"number": 1, "episodes": 1}]})


def test_legacy_plans_are_unchanged():
    result = build_plan({"version": 1, "media": MOVIE, "files": manchester()})
    assert "excluded" not in result and result["errors"]


def test_bd_pack_keeps_episodes_and_ovas_but_drops_previews_and_menus():
    folder = "[64bitsub][Nukitashi][BDRIP_1920x1080][AVC_FLAC]"
    files = [{"path": f"{folder}/[64bitsub][Nukitashi][{e:02d}][BDRIP_1920x1080][AVC_2×FLAC].mkv", "size": 1100 * MB}
             for e in (1, 2)]
    files += [{"path": f"{folder}/[64bitsub][Nukitashi][{name}][BDRIP_1920x1080][AVC_FLAC].mkv", "size": size}
              for name, size in (("OVA_01", 290 * MB), ("YOKOKU_05", 38 * MB), ("PV_01", 85 * MB), ("NCOP", 177 * MB),
                                 ("MENU_01", 19 * MB), ("CM_01", 95 * MB))]
    files.append({"path": f"{folder}/[64bitsub][Nukitashi][OVA_01][BDRIP_1920x1080][AVC_FLAC].cht.ass", "size": 4474})
    result = library(files, {**TV, "seasons": {"0": 4, "1": 12}}, season_number=1)
    assert not result["errors"]
    assert sorted(item["destination"].split("/", 1)[1] for item in result["plan"]) == [
        "Season 00/中头奖还是要上班 S00E01.cht.ass", "Season 00/中头奖还是要上班 S00E01.mkv",
        "Season 01/中头奖还是要上班 S01E01.mkv", "Season 01/中头奖还是要上班 S01E02.mkv"]
    assert {item["role"] for item in result["excluded"]} == {"extra"}
