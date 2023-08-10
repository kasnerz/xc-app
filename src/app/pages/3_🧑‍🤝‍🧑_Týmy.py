#!/usr/bin/env python3

import streamlit as st
import streamlit_authenticator as stauth
import os
import time
import yaml
from yaml.loader import SafeLoader
import time
import pandas as pd
from database import get_database
import accounts
import utils
import re
from unidecode import unidecode
import folium
from streamlit_folium import st_folium

st.set_page_config(page_title="Týmy", page_icon="static/favicon.png", layout="wide")
utils.style_sidebar()
db = get_database()


def backbtn():
    st.experimental_set_query_params()


def parse_links(web):
    if web.startswith("http"):
        return web

    if web.startswith("www"):
        return f"https://{web}"

    # some people post just instagram handles - find all handles and add https://instagram.com/ in front of them

    # find all handles
    handles = re.findall(r"@(\w+)", web)

    if not handles:
        return web

    links = []
    for handle in handles:
        # name = handle.group(1)
        links.append(f"[@{handle}](https://instagram.com/{handle})")

    return ", ".join(links)


def get_pax_link(pax_id, pax_name):
    link_color = db.get_settings_value("link_color")

    return f"<a href='/Účastníci?id={pax_id}' target='_self' style='text-decoration: none; color: {link_color}; margin-top: -10px;'>{pax_name}</a>"


def show_profile(team_id):
    st.button("Zpět", on_click=backbtn)

    team = db.get_team_by_id(team_id)
    if not team:
        st.error("Tým nebyl nalezen.")
        st.stop()

    columns = st.columns([1, 3, 2])

    with columns[1]:
        st.write(f"## {team['team_name']}")

        member_1 = db.get_participant_by_id(team["member1"])
        member_string = get_pax_link(team["member1"], member_1["name"])

        if team["member2"]:
            member_2 = db.get_participant_by_id(team["member2"])
            member_string += ", "
            member_string += get_pax_link(team["member2"], member_2["name"])

        st.markdown(f"<h5>{member_string}</h5>", unsafe_allow_html=True)

        if team["team_motto"]:
            st.write(f"{team['team_motto']}")

        if team["team_web"]:
            links = parse_links(team["team_web"])
            st.markdown(f"🔗 {links}")

        posts = db.get_posts_by_team(team_id)

        if posts.empty:
            st.stop()

        st.divider()
        st.write("#### Příspěvky")

        for i, post in posts.iterrows():
            # link to post
            post_link = f"/?post={post['post_id']}"
            post_date = pd.to_datetime(post["created"]).strftime("%d.%m.%Y %H:%M")
            st.markdown(
                f"{post_date} – <b><a href='{post_link}' target='_self'> {post['action_name']}</a><b>",
                unsafe_allow_html=True,
            )

    with columns[2]:
        photo_path = team["team_photo"]
        if photo_path:
            st.image(db.read_image(photo_path))
        else:
            st.image("static/team.png")


def get_team_name_view(team):
    link_color = db.get_settings_value("link_color")
    name = team["team_name"]
    team_id = team["team_id"]

    link = f"<div><a href='/Týmy?id={team_id}'  target='_self' style='text-decoration: none;'><h5 style='color: {link_color};'>{name}</h5></a></div>"

    return link


def get_member_link(member_id, member_name):
    link_color = db.get_settings_value("link_color")

    return f"<a href='/Účastníci?id={member_id}' style='color: {link_color}; text-decoration: none;' target='_self'>{member_name}</a>"


@st.cache_data(show_spinner=False)
def show_teams():
    teams = db.get_teams()

    if teams.empty:
        st.info("Zatím nemáme žádné týmy. Přihlas se a založ si svůj!")
        st.stop()

    # considering unicode characters in Czech alphabet
    teams = teams.sort_values(by="team_name", key=lambda x: [unidecode(a).lower() for a in x])

    teams_total = len(teams)

    st.caption(f"{teams_total} týmů")

    column_cnt = 4

    for i, (_, team) in enumerate(teams.iterrows()):
        if i % column_cnt == 0:
            cols = st.columns(column_cnt)

        subcol = cols[i % column_cnt]

        with subcol:
            team_name = get_team_name_view(team)
            img_path = team["team_photo"] or "static/team.png"
            img = utils.resize_image(db.read_image(img_path), crop_ratio="1:1")

            member1 = db.get_participant_by_id(team["member1"])
            members = [get_member_link(member1["id"], member1["name"])]

            if team["member2"]:
                member2 = db.get_participant_by_id(team["member2"])
                members.append(get_member_link(member2["id"], member2["name"]))

            members = ", ".join(members)
            st.image(img, width=100)

            st.markdown(f"{team_name}", unsafe_allow_html=True)
            st.markdown(f"<div style='margin-top: -15px; margin-bottom:0px;'>{members}</div>", unsafe_allow_html=True)

            if team["team_motto"]:
                motto = utils.escape_html(team["team_motto"])
                motto = motto[:100] + "..." if len(motto) > 100 else motto

                st.markdown(
                    f"<div style='margin-top: -5px; margin-bottom:30px; font-size:12px; color: grey'>{motto}</div>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown("")


def main():
    params = st.experimental_get_query_params()
    xchallenge_year = db.get_settings_value("xchallenge_year")

    if params.get("id"):
        team_id = params["id"][0]

        show_profile(team_id)
        st.stop()

    st.markdown(f"# Týmy")

    st.markdown(
        """
    <style>
    [data-testid=stImage]{
            text-align: center;
            display: block;
            margin-left: auto;
            margin-right: auto;
        }
    [data-testid=stVerticalBlock]{
            text-align: center;
    }
    [data-baseweb=tab-list] {
        justify-content: center;
    }
    </style>
    """,
        unsafe_allow_html=True,
    )

    show_teams()


if __name__ == "__main__":
    main()
