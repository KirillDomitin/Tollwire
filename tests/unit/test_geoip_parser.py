"""Разбор списка российских префиксов (этап 24.4): RIPE, CIDR, лимиты, пороги, подмена, эффективный список."""

from datetime import datetime, timezone
from ipaddress import IPv4Network

import pytest

from modules.geoip.parser import (
    MAX_PREFIXES,
    MIN_PREFIXES,
    GeoipError,
    addresses_count,
    effective_list,
    is_acceptable,
    needs_confirmation,
    parse_list,
    parse_stored,
    render_list,
    requires_confirmation,
    sha256_of,
    validate_override,
)
from tests.geoip_support import cidr_file, ripe_file, ripe_line, synthetic_networks

BIG = synthetic_networks(3500)


def net(text: str) -> IPv4Network:
    return IPv4Network(text)


def small(text: str, **limits: int) -> list[IPv4Network]:
    """Разбор с малыми порогами: проверяем логику, а не размеры реального файла."""
    return parse_list(text, min_records=1, min_prefixes=1, max_prefixes=100, **limits).prefixes


# --- RIPE ---------------------------------------------------------------------------------------------------------


def test_a_ripe_file_gives_the_collapsed_russian_ipv4_list() -> None:
    result = parse_list(ripe_file(BIG))
    assert result.prefixes == sorted(BIG) and result.records == len(BIG)
    assert result.version == "20240101"  # дата записей, а не заголовка


def test_other_countries_ipv6_asn_summaries_and_the_header_are_ignored() -> None:
    result = parse_list(ripe_file(BIG))
    assert not any(str(network).startswith("5.9.") for network in result.prefixes)
    assert result.skipped == 0  # чужие строки - не мусор


def test_a_count_that_is_not_a_power_of_two_is_split_into_cidrs() -> None:
    text = "ripencc|RU|ipv4|77.88.8.0|768|20240101|allocated\n"  # 3 x /24
    assert small(text) == [net("77.88.8.0/23"), net("77.88.10.0/24")]  # 768 = 512 + 256, первые два склеены
    assert addresses_count(small(text)) == 768


def test_adjacent_blocks_are_merged() -> None:
    text = "ripencc|RU|ipv4|5.45.200.0|256|20240101|allocated\nripencc|RU|ipv4|5.45.201.0|256|20240101|assigned\n"
    assert small(text) == [net("5.45.200.0/23")]


@pytest.mark.parametrize("status", ["reserved", "available", "ALLOCATED_PA", ""])
def test_only_allocated_and_assigned_are_taken(status: str) -> None:
    text = f"ripencc|RU|ipv4|5.45.200.0|256|20240101|{status}\nripencc|RU|ipv4|77.88.8.0|256|20240101|assigned\n"
    assert small(text) == [net("77.88.8.0/24")]


def test_status_case_does_not_matter() -> None:
    assert small("ripencc|RU|ipv4|77.88.8.0|256|20240101|ALLOCATED\n") == [net("77.88.8.0/24")]


@pytest.mark.parametrize(
    "line",
    [
        "ripencc|RU|ipv4|not-an-ip|256|20240101|allocated",
        "ripencc|RU|ipv4|77.88.8.0|abc|20240101|allocated",
        "ripencc|RU|ipv4|77.88.8.0|0|20240101|allocated",
        "ripencc|RU|ipv4|77.88.8.0|-5|20240101|allocated",
        "ripencc|RU|ipv4|255.255.255.0|4096|20240101|allocated",  # выходит за пространство адресов
        "ripencc|RU|ipv4|77.88.8.0|256",
        "garbage line",
        "||||||",
        "ripencc|RU|ipv4|77.88.8.0|99999999999999999999|20240101|allocated",
    ],
)
def test_broken_lines_are_skipped_not_fatal(line: str) -> None:
    result = parse_list(f"{line}\nripencc|RU|ipv4|5.45.200.0|256|20240101|allocated\n", min_records=1, min_prefixes=1)
    assert result.prefixes == [net("5.45.200.0/24")]


def test_prefixes_outside_the_public_internet_are_dropped_and_counted() -> None:
    text = (
        "ripencc|RU|ipv4|10.0.0.0|16777216|20240101|allocated\n"  # частная
        "ripencc|RU|ipv4|127.0.0.0|256|20240101|allocated\n"
        "ripencc|RU|ipv4|192.168.0.0|256|20240101|allocated\n"
        "ripencc|RU|ipv4|224.0.0.0|256|20240101|allocated\n"
        "ripencc|RU|ipv4|0.0.0.0|2147483648|20240101|allocated\n"  # весь пол-интернета
        "ripencc|RU|ipv4|77.88.8.0|256|20240101|allocated\n"
    )
    result = parse_list(text, min_records=1, min_prefixes=1)
    assert result.prefixes == [net("77.88.8.0/24")] and result.rejected == 5 and result.skipped == 0


def test_a_range_wider_than_slash_eight_is_not_accepted() -> None:
    text = "ripencc|RU|ipv4|64.0.0.0|268435456|20240101|allocated\nripencc|RU|ipv4|77.88.8.0|256|20240101|allocated\n"
    assert small(text) == [net("77.88.8.0/24")]
    assert net("64.0.0.0/4") not in small(text)


# --- CIDR -----------------------------------------------------------------------------------------------------------


def test_a_plain_cidr_list_is_accepted_with_comments_and_blank_lines() -> None:
    text = "# список\n\n77.88.8.0/24  # комментарий\n5.45.200.0/24\n"
    assert small(text) == [net("5.45.200.0/24"), net("77.88.8.0/24")]


@pytest.mark.parametrize("line", ["77.88.8.1/24", "77.88.8.0/33", "77.88.8.0", "77.88.8.0/", "2001:db8::/32", "a/b"])
def test_malformed_cidr_lines_are_skipped(line: str) -> None:
    assert small(f"{line}\n5.45.200.0/24\n") == [net("5.45.200.0/24")]


def test_ripe_and_cidr_lines_may_be_mixed() -> None:
    text = ripe_line(net("77.88.8.0/24")) + "\n5.45.200.0/24\n"
    assert small(text) == [net("5.45.200.0/24"), net("77.88.8.0/24")]


# --- пороги и лимиты ------------------------------------------------------------------------------------------------


def test_too_few_records_is_an_error() -> None:
    with pytest.raises(GeoipError, match="Too few valid records"):
        parse_list(ripe_file(synthetic_networks(500)))


def test_too_few_prefixes_after_merging_is_an_error() -> None:
    adjacent = [
        net(f"20.{b}.{c}.0/24") for b in range(0, 10) for c in range(0, 256)
    ]  # 2560 смежных /24 -> 10 блоков /16
    with pytest.raises(GeoipError, match="prefixes after merging"):
        parse_list(ripe_file(adjacent))


def test_too_many_prefixes_is_an_error() -> None:
    with pytest.raises(GeoipError, match="prefixes after merging"):
        parse_list(cidr_file(synthetic_networks(MAX_PREFIXES + 1)))


def test_the_limits_are_the_agreed_ones() -> None:
    assert (MIN_PREFIXES, MAX_PREFIXES) == (3000, 30000)
    assert len(parse_list(cidr_file(synthetic_networks(MIN_PREFIXES))).prefixes) == MIN_PREFIXES
    assert len(parse_list(cidr_file(synthetic_networks(MAX_PREFIXES))).prefixes) == MAX_PREFIXES


def test_an_empty_or_garbage_file_is_an_error_without_quoting_it() -> None:
    for text in ("", "\n\n", "<html>blocked</html>\n" * 5000):
        with pytest.raises(GeoipError) as caught:
            parse_list(text)
        assert "html" not in str(caught.value)


def test_the_version_falls_back_to_today_for_a_plain_list() -> None:
    today = datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert parse_list(cidr_file(BIG), today=today).version == "20261020"


# --- хранение и сравнение ------------------------------------------------------------------------------------------


def test_render_and_parse_stored_roundtrip_and_the_hash_is_stable() -> None:
    text = render_list(BIG)
    assert parse_stored(text) == sorted(BIG) and render_list(reversed(BIG)) == text
    assert sha256_of(text) == sha256_of(render_list(BIG)) and len(sha256_of(text)) == 64


def test_a_corrupted_stored_list_is_refused() -> None:
    with pytest.raises(GeoipError):
        parse_stored("77.88.8.0/24\n10.0.0.0/8\n")
    with pytest.raises(ValueError):
        parse_stored("garbage\n")


def test_confirmation_is_needed_for_a_change_over_twenty_percent_in_either_measure() -> None:
    assert not needs_confirmation(1000, 1_000_000, 1000, 1_000_000)
    assert not needs_confirmation(1000, 1_000_000, 1200, 1_200_000)  # ровно 20%
    assert needs_confirmation(1000, 1_000_000, 1201, 1_000_000)  # префиксов больше
    assert needs_confirmation(1000, 1_000_000, 1000, 799_999)  # покрытых адресов меньше
    assert needs_confirmation(1000, 1_000_000, 700, 1_000_000)
    assert not needs_confirmation(0, 0, 5000, 5_000_000)  # первого списка сравнивать не с чем


# --- исключения администратора -------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["77.88.8.0/24", " 77.88.8.0/24 ", "8.8.8.8/32", "64.0.0.0/16"])
def test_good_override_networks(value: str) -> None:
    assert validate_override(value) == IPv4Network(value.strip())


@pytest.mark.parametrize(
    "value",
    [
        "10.0.0.0/8", "192.168.1.0/24", "172.17.0.0/16", "10.77.0.0/22", "127.0.0.0/8", "224.0.0.0/4", "169.254.0.0/16",
        "0.0.0.0/0", "0.0.0.0/1", "64.0.0.0/4", "77.88.8.1/24", "77.88.8.0/33", "77.88.8.0", "::/0", "x", "",
        "77.88.8.0/24\n10.0.0.0/8", "77.88.8.0/24;reboot",
    ],
)  # fmt: skip
def test_unsafe_or_malformed_override_networks_are_refused(value: str) -> None:
    with pytest.raises(GeoipError):
        validate_override(value)


def test_acceptable_means_public_and_not_wider_than_slash_eight() -> None:
    assert is_acceptable(net("77.88.8.0/24")) and is_acceptable(net("5.0.0.0/12"))
    assert not is_acceptable(net("5.0.0.0/8")) and is_acceptable(net("5.0.0.0/8"), 8)  # RIPE: не крупнее /12
    assert (
        not is_acceptable(net("4.0.0.0/7"))
        and not is_acceptable(net("10.0.0.0/24"))
        and not is_acceptable(net("100.64.0.0/10"))
    )


# --- эффективный список --------------------------------------------------------------------------------------------


def test_effective_list_without_overrides_is_the_active_list() -> None:
    assert effective_list(BIG) == sorted(BIG)


def test_direct_overrides_are_added_and_merged() -> None:
    active = [net("77.88.8.0/25")]
    assert effective_list(active, direct=[net("77.88.8.128/25"), net("5.45.200.0/24")]) == [
        net("5.45.200.0/24"),
        net("77.88.8.0/24"),
    ]


def test_tunnel_overrides_cut_holes_in_big_networks() -> None:
    result = effective_list([net("77.88.8.0/22")], tunnel=[net("77.88.8.64/26")])
    assert addresses_count(result) == 1024 - 64
    assert not any(network.overlaps(net("77.88.8.64/26")) for network in result)
    assert net("77.88.9.0/24") in result and net("77.88.10.0/23") in result


def test_a_tunnel_override_removes_a_whole_prefix_and_what_lies_inside_it() -> None:
    result = effective_list([net("77.88.8.0/24"), net("5.45.200.0/24")], tunnel=[net("77.88.8.0/22")])
    assert result == [net("5.45.200.0/24")]


def test_a_tunnel_override_wins_over_a_direct_one_on_the_same_network() -> None:
    assert effective_list([], direct=[net("77.88.8.0/24")], tunnel=[net("77.88.8.0/24")]) == []


def test_a_direct_override_does_not_resurrect_what_a_tunnel_override_cut() -> None:
    result = effective_list([net("77.88.8.0/24")], direct=[net("77.88.8.0/26")], tunnel=[net("77.88.8.0/25")])
    assert result == [net("77.88.8.128/25")]


def test_the_effective_list_is_deterministic_and_collapsed() -> None:
    a = effective_list(BIG, direct=[net("77.88.8.0/24")], tunnel=[BIG[10]])
    b = effective_list(list(reversed(BIG)), direct=[net("77.88.8.0/24")], tunnel=[BIG[10]])
    assert a == b and a == sorted(a) and BIG[10] not in a and net("77.88.8.0/24") in a


# --- зарезервированные диапазоны: пересечение, а не только крайние адреса (ревью 24.4) ----------------------------

OVERLAPPING = [
    "172.0.0.0/8",  # накрывает 172.16.0.0/12 (сети Docker)
    "192.0.0.0/8",
    "169.0.0.0/8",  # накрывает 169.254.0.0/16 (метаданные облака)
    "100.0.0.0/8",  # накрывает 100.64.0.0/10 (CGNAT)
    "172.0.0.0/9",
    "169.192.0.0/10",  # 169.192-169.255, включая 169.254/16
    "172.16.0.0/12",
    "172.17.0.0/16",
    "100.64.0.0/10",
    "198.18.0.0/15",
    "192.88.99.0/24",
    "0.0.0.0/8",
    "240.0.0.0/4",
]


@pytest.mark.parametrize("value", OVERLAPPING)
def test_networks_overlapping_reserved_ranges_are_not_acceptable(value: str) -> None:
    assert not is_acceptable(net(value), 8) and not is_acceptable(net(value), 16)


@pytest.mark.parametrize("value", OVERLAPPING)
def test_a_direct_override_overlapping_reserved_ranges_is_refused(value: str) -> None:
    with pytest.raises(GeoipError):
        validate_override(value)


def test_a_tunnel_override_may_be_any_valid_network_not_wider_than_slash_eight() -> None:
    assert validate_override("10.0.0.0/8", direct=False) == net("10.0.0.0/8")
    assert validate_override("77.88.8.0/24", direct=False) == net("77.88.8.0/24")
    for bad in ("0.0.0.0/0", "0.0.0.0/7", "77.88.8.1/24", "garbage", ""):
        with pytest.raises(GeoipError):
            validate_override(bad, direct=False)


@pytest.mark.parametrize("value", ["77.88.0.0/15", "77.0.0.0/8", "8.0.0.0/12"])
def test_a_direct_override_is_not_wider_than_slash_sixteen(value: str) -> None:
    with pytest.raises(GeoipError):
        validate_override(value)
    assert validate_override("77.88.0.0/16") == net("77.88.0.0/16")


def test_ripe_networks_overlapping_reserved_ranges_are_dropped_and_counted_not_fatal() -> None:
    text = (
        "ripencc|RU|ipv4|172.0.0.0|16777216|20240101|allocated\n"  # 172.0.0.0/8
        "ripencc|RU|ipv4|169.0.0.0|16777216|20240101|allocated\n"
        "ripencc|RU|ipv4|100.64.0.0|256|20240101|allocated\n"
        "ripencc|RU|ipv4|77.88.8.0|256|20240101|allocated\n"
    )
    result = parse_list(text, min_records=1, min_prefixes=1)
    assert result.prefixes == [net("77.88.8.0/24")] and result.rejected == 3 and result.skipped == 0


def test_ripe_networks_wider_than_slash_twelve_are_dropped() -> None:
    text = (
        "ripencc|RU|ipv4|64.0.0.0|2097152|20240101|allocated\n"  # 64.0.0.0/11
        "ripencc|RU|ipv4|77.88.0.0|65536|20240101|allocated\n"  # /16
        "ripencc|RU|ipv4|80.64.0.0|1048576|20240101|allocated\n"  # /12 - ещё можно
    )
    result = parse_list(text, min_records=1, min_prefixes=1)
    assert result.prefixes == [net("77.88.0.0/16"), net("80.64.0.0/12")] and result.rejected == 1


def test_a_strict_upload_with_a_reserved_network_is_refused_as_a_whole() -> None:
    text = "10.0.0.0/8\n" + cidr_file(BIG)
    assert parse_list(text).rejected == 1  # RIPE / обычный разбор: отбросили и пошли дальше
    with pytest.raises(GeoipError, match="cannot be routed directly"):
        parse_list(text, strict=True)
    assert parse_list(cidr_file(BIG), strict=True).rejected == 0


def test_the_effective_list_never_contains_reserved_ranges_even_from_a_dirty_store() -> None:
    result = effective_list([net("172.0.0.0/8"), net("77.88.8.0/24")], direct=[net("100.0.0.0/9")])
    covered = [
        network
        for network in result
        if network.overlaps(net("172.16.0.0/12")) or network.overlaps(net("100.64.0.0/10"))
    ]
    assert covered == []
    assert net("77.88.8.0/24") in result and net("100.0.0.0/10") in result
    assert net("172.0.0.0/12") in result  # куски 172/8 вне 172.16/12 остаются (они публичные)
    assert net("172.32.0.0/11") in result


def test_hosts_of_the_gateway_and_its_exits_are_cut_out_of_the_effective_list() -> None:
    result = effective_list(
        [net("77.88.8.0/24")], direct=[net("5.45.200.0/24")], protected=[net("77.88.8.7/32"), net("5.45.200.1/32")]
    )
    assert net("77.88.8.7/32") not in result
    for network in result:
        assert not network.overlaps(net("77.88.8.7/32")) and not network.overlaps(net("5.45.200.1/32"))
    assert sum(n.num_addresses for n in result) == 256 - 1 + 256 - 1


# --- порог изменения с учётом пересечения ---------------------------------------------------------------------------


def block(first: int, count: int) -> list[IPv4Network]:
    """count несмежных /24 в 20.<first>.x.0 - 20.<...>: сети из разных диапазонов не пересекаются."""
    return [net(f"20.{first + index // 100}.{(index % 100) * 2}.0/24") for index in range(count)]


def test_the_same_list_or_a_small_change_needs_no_confirmation() -> None:
    old = block(0, 1000)
    assert not requires_confirmation(old, old)
    assert not requires_confirmation(old, [*old[50:], *block(50, 50)])  # заменено 5%


def test_replacing_a_part_with_foreign_networks_at_the_same_sums_needs_confirmation() -> None:
    old = block(0, 1000)
    swapped = [*old[300:], *block(60, 300)]  # столько же префиксов и адресов, но 30% - чужие
    assert len(swapped) == len(old)
    assert requires_confirmation(old, swapped)


def test_each_side_of_the_intersection_rule_is_checked() -> None:
    old = block(0, 1000)
    # осталось 79% старого (меньше 80%), новых адресов 0%: сумма уменьшилась
    assert requires_confirmation(old, old[:790])
    # осталось 100% старого, новых 20,1% адресов
    assert requires_confirmation(old, [*old, *block(70, 201)])
    # ровно на границе: осталось 80% старого и новых 0% -> но размер -20% (граница включена)
    assert not requires_confirmation(old, old[:800])
    # заменено 19%: осталось 81%, новых ~19% -> без подтверждения
    assert not requires_confirmation(old, [*old[190:], *block(70, 190)])


def test_intersection_counts_addresses_not_prefixes() -> None:
    old = [net("20.0.0.0/16"), net("21.0.0.0/24")]
    new = [net("20.0.0.0/17"), net("21.0.0.0/24")]  # половина большого блока ушла: осталось ~50% адресов
    assert requires_confirmation(old, new)
    assert not requires_confirmation(old, old)
