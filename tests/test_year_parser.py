"""Tests for the year-from-filename parser."""

from pathlib import Path

from njlead.ingest.year_parser import (
    parse_year_from_filename,
    parse_year_from_path,
)

# --- 4-digit years ---

def test_bridgeton_month_year():
    assert parse_year_from_filename("Bridgeton BOE 6-2022.pdf") == 2022


def test_wayne_hills_plain():
    assert parse_year_from_filename("Wayne Hills 2024.pdf") == 2024


def test_westmilford_with_suffix():
    assert parse_year_from_filename("westmilford2025-3.pdf") == 2025


# --- School-year ranges ---

def test_school_year_range_21_22():
    assert parse_year_from_filename("21-22 Lead Testing Results.pdf") == 2022


def test_school_year_range_24_25():
    assert parse_year_from_filename("School District 24-25 Results.pdf") == 2025


# --- Date patterns ---

def test_date_pattern_mdy():
    assert parse_year_from_filename("Maywood_Results_1-21-25.pdf") == 2025


# --- 2-digit bare years ---

def test_wanaque_bare_2digit():
    assert parse_year_from_filename("wanaque24.pdf") == 2024


# --- Lab project numbers should NOT match ---

def test_lab_project_not_year():
    # "24-337" is a lab project number, not a school year
    assert parse_year_from_filename("Report_24-337.pdf") != 2024 or parse_year_from_filename("Report_24-337.pdf") is None


def test_lab_project_5digit():
    # "25156-01" should not match as year 2025
    result = parse_year_from_filename("Lab_25156-01.pdf")
    assert result is None or result != 2025


# --- Folder path ---

def test_folder_path_2digit():
    p = Path("C:/data/Newark/22/report.pdf")
    assert parse_year_from_path(p) == 2022


def test_folder_path_4digit():
    p = Path("C:/data/2024/Bergen/report.pdf")
    assert parse_year_from_path(p) == 2024


def test_folder_path_no_year():
    p = Path("C:/data/Newark/report.pdf")
    assert parse_year_from_path(p) is None
