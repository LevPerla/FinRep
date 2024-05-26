from sys import path
path.append('/Users/levperla/PycharmProjects/FinRep')

import flet as ft
import webbrowser
from src.flatter_app.views.main_view import add_main_view
from src.flatter_app.views.add_spandings_view import add_spandings_view
from src.flatter_app.views.report_gen_view import add_report_gen_view
from src.flatter_app.views.main_report_view import add_main_report_view


def main(page: ft.Page):
    page.title = "Lev's finance app"
    page.window_height = 750
    page.window_width = 500
    page.bgcolor = ft.colors.WHITE
    
    # Theme
    page.fonts = {
        "Kanit": "https://raw.githubusercontent.com/google/fonts/master/ofl/kanit/Kanit-Bold.ttf",
        "Open Sans": "/fonts/OpenSans-Regular.ttf"
    }
    page.theme_mode = ft.ThemeMode.DARK
    page.theme = ft.theme.Theme(color_scheme_seed="green")
    # page.theme = ft.Theme(font_family="Kanit")
    
    # Scrolls
    page.auto_scroll = True
    page.scroll = ft.ScrollMode.HIDDEN
    
    # Check resize params
    def page_resize(e):
        print("New page size:", page.window_width, page.window_height)
    page.on_resize = page_resize
    
    
    def route_change(route):
        page.views.clear()
        
        # Add main view of app  
        page.views.append(add_main_view(page))  
        
        # add spendings view
        if page.route == "/add_spendings":
            page.views.append(add_spandings_view(page))

        # Report generation view
        if page.route == "/report_generation":
            page.views.append(add_main_report_view(page))
            # page.views.append(add_report_gen_view(page))
        
        print(page.route)
        page.update()    
    
    def view_pop(view):
        page.views.pop()
        top_view = page.views[-1]
        page.go(top_view.route)

    page.on_route_change = route_change
    page.on_view_pop = view_pop
    page.go(page.route)

if __name__ == '__main__':
    ft.app(target=main,
           view=ft.WEB_BROWSER,
           port=5055
           )