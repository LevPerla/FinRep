from src.dashboard.chart_style import EXPENSE_CATEGORY_COLORS, expense_category_color


def test_known_expense_categories_have_stable_distinct_colors():
    colors = [expense_category_color(category) for category in EXPENSE_CATEGORY_COLORS]
    assert len(colors) == len(set(colors)) == 10
    assert expense_category_color("Жильё") == expense_category_color("Жилье")
    assert expense_category_color("Будущая категория") == expense_category_color("Будущая категория")
