#!/usr/bin/env python3

from datetime import datetime
from PIL import Image, ImageOps
from slugify import slugify
from unidecode import unidecode
from woocommerce import API
from accounts import AccountManager
import argparse
import boto3
import csv
import io
import json
import logging
import mimetypes
import os
import pandas as pd
import re
import requests
import s3fs
import sqlite3
import streamlit as st
import time
import traceback
import utils
import yaml
import zipfile

# logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

current_dir = os.path.dirname(os.path.realpath(__file__))


# TODO find a way how to retrieve this value from the config
TTL = 600


@st.cache_resource
def get_database():
    return Database()


class Database:
    def __init__(self):
        self.settings_path = os.path.join(current_dir, "settings.yaml")
        self.load_settings()

        self.wcapi = API(
            url="https://x-challenge.cz/",
            consumer_key=st.secrets["woocommerce"]["consumer_key"],
            consumer_secret=st.secrets["woocommerce"]["consumer_secret"],
            version="wc/v3",
            timeout=30,
        )
        self.xchallenge_year = str(self.get_settings_value("xchallenge_year"))
        os.makedirs(os.path.join("db", self.xchallenge_year), exist_ok=True)
        self.db_path = os.path.join("db", self.xchallenge_year, "database.db")

        if self.get_settings_value("file_system") == "s3":
            # S3 bucket
            # used as a filesystem for the database
            self.fs = s3fs.S3FileSystem(anon=False)
            self.fs_bucket = self.get_settings_value("fs_bucket")
            self.boto3 = boto3.resource(
                "s3",
                region_name=st.secrets["AWS_DEFAULT_REGION"],
                aws_access_key_id=st.secrets["AWS_ACCESS_KEY_ID"],
                aws_secret_access_key=st.secrets["AWS_SECRET_ACCESS_KEY"],
            )
        self.top_dir = f"files/{self.xchallenge_year}"
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.create_tables()

        self.am = AccountManager()
        self.preauthorized_emails = self.load_preauthorized_emails()

        print("Database initialized")

    def __del__(self):
        self.conn.close()

    def load_settings(self):
        with open(self.settings_path) as f:
            self.settings = yaml.load(f, Loader=yaml.FullLoader)

    def get_settings_value(self, key):
        return self.settings.get(key)

    def set_settings_value(self, key, value):
        self.settings[key] = value
        self.save_settings()

    def save_settings(self):
        with open(self.settings_path, "w") as f:
            yaml.dump(self.settings, f)

    def get_settings_as_df(self):
        settings = [{"key": key, "value": value} for key, value in self.settings.items()]
        df = pd.DataFrame(settings)
        return df

    def save_settings_from_df(self, df):
        for key, value in zip(df["key"], df["value"]):
            self.settings[key] = value

        self.save_settings()

    def restore_backup(self, backup_file):
        zip_path = os.path.join("backups", backup_file)

        if not os.path.exists(zip_path):
            raise ValueError(f"Backup file {zip_path} does not exist.")

        # overwrite the database in db folder by unzipping the backup
        # the zip file contains the innards of the db folder
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall("db")

        # reload the database
        self.__init__()
        utils.clear_cache()

    @st.cache_resource(ttl=TTL)
    def get_boto3_object(_self, filepath):
        print("get_boto3_object")
        obj = _self.boto3.Object(_self.fs_bucket, filepath)
        return obj

    @st.cache_resource(ttl=TTL, max_entries=250)
    def read_file(_self, filepath, mode="b"):
        fs = _self.get_settings_value("file_system")
        if fs == "s3" and not filepath.startswith("static/"):
            # use boto3 to get the S3 object

            try:
                obj = _self.get_boto3_object(filepath)
                # read the contents of the file and return
                content = obj.get()["Body"].read()

                # text files need decoding
                if mode == "t":
                    return content.decode("utf-8")

                return content
            except Exception as e:
                print(traceback.format_exc())
                return None

        elif fs == "local" or filepath.startswith("static/"):
            with open(filepath, "r" + mode) as f:
                return f.read()
        else:
            raise ValueError(f"Unknown file system: {fs}, use s3 or local.")

    @st.cache_resource(ttl=TTL, max_entries=250)
    def read_image(_self, filepath):
        img = _self.read_file(filepath, mode="b")

        if not img:
            print("Cannot load image")
            # return blank image
            return Image.new("RGB", (1, 1))

        # read image using PIL
        img = Image.open(io.BytesIO(img))
        img = ImageOps.exif_transpose(img)

        return img

    def write_file(self, filepath, content):
        fs = self.get_settings_value("file_system")

        if fs == "s3":
            self.boto3.Object(self.fs_bucket, filepath).put(Body=content)
        elif fs == "local":
            mode = "t" if type(content) == str else "b"

            os.makedirs(os.path.dirname(filepath), exist_ok=True)

            with open(filepath, "w" + mode) as f:
                return f.write(content)

    def wc_get_all_orders(self, product_id):
        page = 1
        orders = []
        has_more_orders = True

        while has_more_orders:
            params = {
                "product": product_id,
                # "per_page": 10,
                "per_page": 100,
                "page": page,
            }
            # Make the API request to retrieve a batch of orders

            response = self.wcapi.get("orders", params=params)
            response = response.json()

            # Check if there are any orders in the response
            if len(response) > 0:
                # Add the retrieved orders to the list
                orders.extend(response)

                # Increment the page number for the next request
                page += 1

                # print("DEBUG! TODO remove has_more_orders = False for fetching all orders")
                # has_more_orders = False
            else:
                # No more orders, exit the loop
                has_more_orders = False

        return orders

    def wc_fetch_participants(self, product_id, log_area=None, limit=None):
        new_participants = []
        logger.info("Fetching participants from WooCommerce...")

        # print all existing participants
        for row in self.conn.execute("SELECT * FROM participants"):
            print(dict(row))

        # TODO fetch only orders after last update
        orders = self.wc_get_all_orders(product_id=product_id)
        print(f"Found {len(orders)} orders")

        if log_area:
            st.write(f"Nalezeno {len(orders)} objednávek")
            pb = st.progress(value=0, text="Načítám info o účastnících")

        user_ids = [user["customer_id"] for user in orders][:limit]

        for i, user_id in enumerate(user_ids):
            logger.info(f"Fetching user {user_id}")
            response = self.wcapi.get("customers/" + str(user_id))
            response = response.json()
            new_participants.append(response)

            if log_area:
                total_paxes = len(user_ids)
                if limit:
                    total_paxes = min(limit, total_paxes)

                pb.progress(i / float(len(user_ids)), f"Načítám info o účastnících ({i}/{total_paxes})")

        with open("wc_participants.json", "w") as f:
            json.dump(new_participants, f)

        self.add_participants(new_participants)
        self.load_preauthorized_emails()

        utils.clear_cache()

        pb.progress(1.0, "Hotovo!")

    def load_preauthorized_emails(self):
        participants = self.get_participants(include_non_registered=True)

        emails = []
        if not participants.empty:
            # extract the field `email` from pandas df
            emails = list(participants.email)

        extra_allowed_emails = list(self.am.get_extra_accounts().keys())
        preauthorized = {"emails": [e.lower() for e in emails + extra_allowed_emails]}

        return preauthorized

    def get_preauthorized_emails(self):
        return self.preauthorized_emails

    def add_extra_participant(self, email, name):
        # generate a random integer id
        user_id = utils.generate_uuid()
        email = email.lower()

        self.conn.execute(
            "INSERT OR IGNORE INTO participants (id, email, name_web) VALUES (?, ?, ?)",
            (user_id, email, name),
        )
        self.conn.commit()

    def add_participants(self, new_participants):
        for user in new_participants:
            user_data = (
                str(int(user["id"])),
                user["email"],
                user["first_name"].title() + " " + user["last_name"].title(),
            )

            self.conn.execute(
                "INSERT OR IGNORE INTO participants (id, email, name_web) VALUES (?, ?, ?)",
                user_data,
            )
            self.conn.commit()

    def wc_get_user_by_email(self, email):
        query = "SELECT * FROM participants WHERE email = ?"
        return self.conn.execute(query, (email,)).fetchone()

    def get_participants(self, sort_by_name=True, include_non_registered=False, fetch_teams=False):
        # the table `participants` include only emails
        # we need to join this with the user accounts

        participants = []
        query = "SELECT * FROM participants"

        for pax_info in self.conn.execute(query).fetchall():
            pax_info = dict(pax_info)
            user_info = self.am.get_user_by_email(pax_info["email"])
            if user_info:
                pax_info.update(user_info)

            if not pax_info.get("name"):
                pax_info["name"] = pax_info["name_web"]

            if not pax_info.get("username"):
                pax_info["username"] = pax_info["name"]

            # if `return_non_registered` is true, return all participants, otherwise only those for which we have info
            if include_non_registered or user_info:
                participants.append(pax_info)

        participants = pd.DataFrame(participants)

        if not participants.empty and sort_by_name:
            # considering unicode characters in Czech alphabet
            participants = participants.sort_values(by="name", key=lambda x: [unidecode(a) for a in x])

        if fetch_teams and not participants.empty:
            teams = self.get_table_as_df("teams")
            # participant is either member1 or member2, if not - no team
            pax_id_to_team = {str(row["member1"]): row for _, row in teams.iterrows() if row["member1"]}
            pax_id_to_team.update({str(row["member2"]): row for _, row in teams.iterrows() if row["member2"]})

            participants["team_name"] = participants.apply(
                lambda x: pax_id_to_team.get(str(x["id"]), {}).get("team_name"), axis=1
            )
            participants["team_id"] = participants.apply(
                lambda x: pax_id_to_team.get(str(x["id"]), {}).get("team_id"), axis=1
            )

        return participants

    def is_participant(self, email):
        query = "SELECT * FROM participants WHERE email = ?"
        return self.conn.execute(query, (email,)).fetchone() is not None

    def get_participant_by_id(self, id):
        query = "SELECT * FROM participants WHERE id = ?"
        pax_info = self.conn.execute(query, (id,)).fetchone()

        if not pax_info:
            return None

        pax_info = dict(pax_info)
        user_info = self.am.get_user_by_email(pax_info["email"])
        if user_info:
            pax_info.update(user_info)

        if not pax_info.get("name"):
            pax_info["name"] = pax_info["name_web"]

        if not pax_info.get("username"):
            pax_info["username"] = pax_info["name"]

        return pax_info

    def get_participant_by_email(self, email):
        query = "SELECT * FROM participants WHERE email = ?"
        return self.conn.execute(query, (email,)).fetchone()

    def update_participant(self, username, email, bio, emergency_contact, photo=None):
        if photo is None:
            query = "UPDATE participants SET bio = ?, emergency_contact = ? WHERE email = ?"
            self.conn.execute(query, (bio, emergency_contact, email))
            self.conn.commit()

        else:
            query = "UPDATE participants SET bio = ?, emergency_contact = ?, photo = ? WHERE email = ?"

            dir_path = os.path.join(self.top_dir, "participants", slugify(username))
            photo_path = os.path.join(dir_path, photo.name)

            self.write_file(filepath=os.path.join(dir_path, photo.name), content=photo.read())

            self.conn.execute(query, (bio, emergency_contact, photo_path, email))
            self.conn.commit()

    def get_table_as_df(self, table_name):
        df = pd.read_sql_query(f"SELECT * FROM {table_name}", self.conn)
        return df

    def save_df_as_table(self, df, table_name):
        # remove rows containing **ONLY** NaNs
        df = df.dropna(how="all")

        df.to_sql(table_name, self.conn, if_exists="replace", index=False)

    def get_post_by_id(self, post_id):
        query = "SELECT * FROM posts WHERE post_id = ?"
        post = self.conn.execute(query, (post_id,)).fetchone()
        post = dict(post)
        post["files"] = json.loads(post["files"])
        return post

    def save_post(self, user, action_type, action, comment, files):
        team = self.get_team_for_user(user["pax_id"])
        # save all the files to the filesystem

        title = action if action_type == "story" else action["name"]

        dir_path = os.path.join(self.top_dir, action_type, title, slugify(team["team_name"]))

        for file in files:
            self.write_file(filepath=os.path.join(dir_path, file.name), content=file.read())

        post_id = utils.generate_uuid()
        files = [{"path": os.path.join(dir_path, file.name), "type": file.type} for file in files]

        # serialize files as json string
        files = json.dumps(files)
        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        pax_id = user["pax_id"]
        team_id = team["team_id"]

        self.conn.execute(
            f"INSERT INTO posts (post_id, pax_id, team_id, action_type, action_name, comment, files, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (post_id, pax_id, team_id, action_type, title, comment, files, created),
        )
        self.conn.commit()

    def get_team_by_id(self, team_id):
        # retrieve team from the database, return a single Python object or None
        query = "SELECT * FROM teams WHERE team_id = ?"
        ret = self.conn.execute(query, (team_id,))
        team = ret.fetchone()
        return team

    def get_team_for_user(self, pax_id):
        if pax_id is None:
            return None

        # the field `member1`, `member2`  contains the id
        query = "SELECT * FROM teams WHERE member1 = ? OR member2 = ?"
        ret = self.conn.execute(query, (pax_id, pax_id))
        team = ret.fetchone()

        return team

    def get_action(self, action_type, action_name):
        # retrieve action from the database, return a single Python object or None
        table_name = {
            "challenge": "challenges",
            "checkpoint": "checkpoints",
        }[action_type]

        if not table_name:
            return None

        # get action that starts with `action_name`
        # solves "Denní výzva #1" vs. "Denní výzva #1 - blabla"
        query = f"SELECT * FROM {table_name} WHERE name LIKE ?"
        ret = self.conn.execute(query, (action_name + "%",))
        action = ret.fetchone()
        return dict(action) if action else None

    def get_points_for_action(self, action_type, action_name):
        if action_type == "story":
            return 0

        action = self.get_action(action_type, action_name)

        if not action:
            print(f"Action {action_name} not found")
            return 0

        pts = action.get("points", 0)

        return pts

    def get_team_overview(self, team_id, posts, participants):
        # filter posts by team_id
        posts_team = posts[posts["team_id"] == team_id]

        # for each post, find a particular action by its `action_name` in the table `challenges`, `checkpoints`, etc. (determined according its action_type) and add the number of points to the post

        # Use the apply() function to apply the get_points function to each row in the DataFrame
        if not posts_team.empty:
            posts_team["points"] = posts_team.apply(
                lambda row: self.get_points_for_action(row["action_type"], row["action_name"]), axis=1
            )

        team = self.get_team_by_id(team_id)

        member1_name = participants[participants["id"] == team["member1"]].to_dict("records")[0]
        member1_name = member1_name["name"]

        member2_name = ""
        if team["member2"]:
            member2_name = participants[participants["id"] == team["member2"]].to_dict("records")[0]
            member2_name = member2_name["name"]

        team_info = {
            "team_id": team_id,
            "team_name": team["team_name"],
            "member1": team["member1"],
            "member1_name": member1_name,
            "member2": team["member2"],
            "member2_name": member2_name,
            "points": posts_team["points"].sum() if not posts_team.empty else 0,
            "posts": posts_team,
        }
        return team_info

    def get_teams_overview(self):
        teams = self.get_table_as_df("teams")
        posts = self.get_table_as_df("posts")
        participants = self.get_participants(include_non_registered=True, sort_by_name=False)

        # get team overview for each team
        teams_info = [self.get_team_overview(team_id, posts, participants) for team_id in teams["team_id"]]

        return teams_info

    def get_posts(self, team_filter):
        # team_filter is the team_name, the table posts contain only id -> join with teams table to get the team_name
        posts = self.get_table_as_df("posts")
        teams = self.get_table_as_df("teams")
        posts = posts.merge(teams, on="team_id")

        if team_filter:
            posts = posts[posts["team_name"] == team_filter]

        # convert files from json string to Python object
        posts["files"] = posts["files"].apply(lambda x: json.loads(x))
        # convert df to list with dicts
        posts = posts.to_dict("records")
        return posts

    def get_available_actions(self, user, action_type):
        # retrieve actions (of type "challenge", etc.) which the user has not yet completed
        # return as list of dicts

        # get user's team
        team_id = None
        team = self.get_team_for_user(user["pax_id"])

        if team:
            team_id = team["team_id"]

        # get all the actions completed by `team_id` in the table `posts`
        completed_actions = self.get_table_as_df("posts")

        completed_actions = completed_actions[completed_actions["action_type"] == action_type]
        completed_actions = completed_actions[completed_actions["team_id"] == team_id]
        completed_actions = completed_actions["action_name"].unique()

        # get all the actions of type `challenge_type` from the table `actions`
        if action_type == "challenge":
            available_actions = self.get_table_as_df("challenges")
        elif action_type == "checkpoint":
            available_actions = self.get_table_as_df("checkpoints")

        available_actions = available_actions[~available_actions["name"].isin(completed_actions)]

        # convert df to list with dicts
        available_actions = available_actions.to_dict("records")

        return available_actions

    def get_teams(self):
        # retrieve all teams from the database, return as pandas df
        return pd.read_sql_query("SELECT * FROM teams", self.conn)

    def get_available_participants(self, pax_id, team):
        all_paxes = self.get_participants(fetch_teams=True, sort_by_name=True, include_non_registered=True)

        if all_paxes.empty:
            return []

        # remove the current user (they are not available for themselves)
        all_paxes = all_paxes[all_paxes["id"] != pax_id]

        if team:
            # find a teammate for the current user
            teammate = team["member1"] if team["member1"] != pax_id else team["member2"]
        else:
            teammate = None

        # no team or teammate
        available_paxes = all_paxes[all_paxes["team_id"].isnull()]

        # prepend the "nobody option"
        available_paxes = pd.concat(
            [
                pd.DataFrame(
                    {
                        "id": ["-1"],
                        "name": ["(bez parťáka)"],
                    }
                ),
                available_paxes,
            ],
            ignore_index=True,
        )

        if teammate:
            # teammate is not in the list because they are already in a team, but we want to show them as available
            teammate_row = all_paxes[all_paxes["id"] == teammate]
            available_paxes = pd.concat([teammate_row, available_paxes], ignore_index=True)

        return available_paxes

    def add_or_update_team(
        self, team_name, team_motto, team_web, team_photo, first_member, second_member, current_team=None
    ):
        # if team is already in the database, get its id
        if current_team:
            team_id = current_team["team_id"]
        else:
            # add team to the database
            team_id = utils.generate_uuid()

        if str(second_member) == "-1":
            second_member = None

        photo_path = None
        if team_photo:
            photo_dir = os.path.join(self.top_dir, "teams", slugify(team_name))
            photo_path = os.path.join(photo_dir, team_photo.name)
            self.write_file(filepath=photo_path, content=team_photo.read())

        self.conn.execute(
            f"INSERT OR REPLACE INTO teams (team_id, team_name, team_motto, team_web, team_photo, member1, member2) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (team_id, team_name, team_motto, team_web, photo_path, first_member, second_member),
        )
        self.conn.commit()

    def create_tables(self):
        self.conn.execute(
            """CREATE TABLE if not exists participants (
                id text not null unique,
                email text not null,
                name_web text not null,
                bio text,
                emergency_contact text,
                photo text,
                primary key(id)       
            );"""
        )
        self.conn.execute(
            """CREATE TABLE if not exists teams (
                team_id text not null unique,
                team_name text not null,
                member1 text not null,
                member2 text,
                member3 text,
                team_motto text,
                team_web text,
                team_photo text,
                location_visibility integer default 1,
                primary key(team_id)       
            );"""
        )
        self.conn.execute(
            """CREATE TABLE if not exists posts (
                post_id text not null unique,
                pax_id text not null,
                team_id text,
                action_type text not null,
                action_name text not null,
                comment text not null,
                created text not null,
                files text not null,
                primary key(post_id)  
            );"""
        )
        self.conn.execute(
            """CREATE TABLE if not exists locations (
                username text not null,
                team_id text,
                comment text,
                longitude float not null,
                latitude float not null,
                accuracy text,
                altitude text,
                altitude_accuracy text,
                heading text,
                speed text,
                date text not null
            );"""
        )
        self.conn.execute(
            """CREATE TABLE if not exists challenges (
                name text not null unique,
                description text not null,
                category text not null,
                points int not null,
                primary key(name)       
            );"""
        )
        self.conn.execute(
            """CREATE TABLE if not exists checkpoints (
                name text not null unique,
                description text not null,
                points int not null,
                latitude float,
                longitude float,
                challenge text,
                primary key(name)       
            );"""
        )
        self.conn.execute(
            """CREATE TABLE if not exists notifications (
                text text not null,
                type text
            );"""
        )

    def save_location(
        self, user, comment, longitude, latitude, accuracy, altitude, altitude_accuracy, heading, speed, date
    ):
        team = self.get_team_for_user(user["pax_id"])
        username = user["username"]
        team_id = team["team_id"]

        self.conn.execute(
            f"INSERT INTO locations (username, team_id, comment, longitude, latitude, accuracy, altitude, altitude_accuracy, heading, speed, date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                username,
                team_id,
                comment,
                longitude,
                latitude,
                accuracy,
                altitude,
                altitude_accuracy,
                heading,
                speed,
                date,
            ),
        )
        self.conn.commit()

    def get_last_location(self, team):
        team_id = team["team_id"]

        df = pd.read_sql_query(
            f"SELECT * FROM locations WHERE team_id='{team_id}' AND team_id='{team_id}' ORDER BY date DESC LIMIT 1",
            self.conn,
        )

        if df.empty:
            return None

        return df.to_dict("records")[0]

    def is_team_visible(self, team):
        team_id = team["team_id"]

        df = pd.read_sql_query(
            f"SELECT * FROM teams WHERE team_id='{team_id}'",
            self.conn,
        )

        if df.empty:
            return None

        return bool(df.to_dict("records")[0]["location_visibility"])

    def toggle_team_visibility(self, team):
        team_id = team["team_id"]

        df = pd.read_sql_query(
            f"SELECT * FROM teams WHERE team_id='{team_id}'",
            self.conn,
        )
        if df.empty:
            return None

        visibility = df.to_dict("records")[0]["location_visibility"]
        visibility = 1 - visibility

        self.conn.execute(
            f"UPDATE teams SET location_visibility={visibility} WHERE team_id='{team_id}'",
        )
        self.conn.commit()

        return visibility

    def find_files_2022(self, action_type, action_name, team_id):
        files = []
        mt = mimetypes.MimeTypes()

        try:
            team_name = self.get_team_by_id(team_id)

            if not team_name:
                print(f"Team {team_id} not found")
                return files
            team_name = team_name["team_name"]

            path = os.path.join("files", "2022", action_type, slugify(action_name), slugify(team_name))

            # find all files in the directory
            for file in os.listdir(path):
                if file.endswith(".txt") or os.path.isdir(os.path.join(path, file)):
                    continue

                file_path = os.path.join(path, file)
                file_type = mt.guess_type(path)[0]

                if not file_type:
                    # guess from the extension
                    extension = file.split(".")[-1]
                    file_type = (
                        f"video/{extension}" if extension.lower() in ["mp4", "mov", "avi"] else f"image/{extension}"
                    )

                files.append({"path": file_path, "type": file_type})
        except FileNotFoundError:
            pass
            # print(f"No files found: {action_type}, {action_name}, {team_id}")

        return files

    def insert_data_2022(self):
        with open("files/2022/prihlasky.csv") as f:
            reader = csv.DictReader(f, delimiter=",")
            for i, row in enumerate(reader):
                # note that we use here member names as ids
                self.conn.execute(
                    """INSERT OR IGNORE INTO teams
                    (team_id, team_name, member1, member2)
                    VALUES
                    (?, ?, ?, ?);
                    """,
                    (
                        i + 1,
                        row["Název týmu"],
                        row["Člen #1: Jméno a příjmení"],
                        row["Člen #2: Jméno a příjmení"],
                    ),
                )
                self.conn.execute(
                    """INSERT OR IGNORE INTO participants
                    (id, email, name_web)
                    VALUES
                    (?, ?, ?);
                    """,
                    (
                        row["Člen #1: Jméno a příjmení"],
                        utils.generate_uuid() + "@xc-test.cz",
                        row["Člen #1: Jméno a příjmení"],
                    ),
                )
                self.conn.execute(
                    """INSERT OR IGNORE INTO participants
                    (id, email, name_web)
                    VALUES
                    (?, ?, ?);
                    """,
                    (
                        row["Člen #2: Jméno a příjmení"],
                        utils.generate_uuid() + "@xc-test.cz",
                        row["Člen #2: Jméno a příjmení"],
                    ),
                )
            self.conn.commit()

        with open("files/2022/odpovedi.csv") as f:
            reader = csv.DictReader(f, delimiter=",")
            for i, row in enumerate(reader):
                username = "xc-bot"
                team_id = row["ID týmu"]

                # format from %d/%m/%Y %H:%M:%S to %Y-%m-%d %H:%M:%S")
                timestamp = row["Timestamp"]

                action_type_dict = {
                    "⭐ splněnou výzvu": "challenge",
                    "📍 splněný checkpoint": "checkpoint",
                    "✍️ příspěvek": "story",
                }
                action_type = action_type_dict[row["Chci přidat..."]]

                if action_type == "challenge":
                    action_name = row["Výzva"]
                    comment = row["Komentář - Výzva"]
                elif action_type == "checkpoint":
                    action_name = row["Checkpoint"]
                    comment = row["Komentář - Checkpoint"]
                elif action_type == "story":
                    action_name = row["Nadpis"]
                    comment = row["Text"]

                files = self.find_files_2022(action_type, action_name, team_id)
                files = json.dumps(files)

                self.conn.execute(
                    """INSERT OR IGNORE INTO posts
                    (post_id, username, team_id, action_type, action_name, comment, created, files)
                    VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        utils.generate_uuid(),
                        username,
                        team_id,
                        action_type,
                        action_name,
                        comment,
                        timestamp,
                        files,
                    ),
                )
            self.conn.commit()

        with open("files/2022/challenges.csv") as f:
            reader = csv.DictReader(f, delimiter=",")
            for i, row in enumerate(reader):
                self.conn.execute(
                    """INSERT OR IGNORE INTO challenges
                    (name, category, description, points)
                    VALUES
                    (?, ?, ?, ?);
                    """,
                    (
                        row["název"],
                        row["kategorie"],
                        row["popis"],
                        row["počet bodů"],
                    ),
                )
            self.conn.commit()

        with open("files/2022/checkpoints.csv") as f:
            reader = csv.DictReader(f, delimiter=",")
            for i, row in enumerate(reader):
                gps = row["gps"]

                try:
                    # remove all letters
                    gps = re.sub("[a-zA-Z]", "", gps)
                    gps = gps.split(",")[:2]

                    lat = float(gps[0].strip())
                    lon = float(gps[1].strip())
                except:
                    print("Cannot convert", gps)
                    gps = None

                self.conn.execute(
                    """INSERT OR IGNORE INTO checkpoints
                    (name, description, points, challenge, latitude, longitude)
                    VALUES
                    (?, ?, ?, ?, ?, ?);
                    """,
                    (
                        row["název"],
                        row["popis"],
                        row["počet bodů"],
                        row["výzva (dobrovolná)"],
                        lat,
                        lon,
                    ),
                )
            self.conn.commit()


if __name__ == "__main__":
    # read arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--load_from_wc_product", type=int)
    parser.add_argument("-f", "--load_from_local_file", type=str)
    parser.add_argument("--insert_data_2022", action="store_true")

    args = parser.parse_args()

    print(args)

    print("Creating database...")
    db = Database()
    db.create_tables()

    if args.insert_data_2022:
        db.insert_data_2022()

    if args.load_from_wc_product:
        print("Fetching participants from Woocommerce...")
        db.wc_fetch_participants(product_id=args.load_from_wc_product)
    elif args.load_from_local_file:
        print("Loading users from file...")
        with open(args.load_from_local_file) as f:
            wc_participants = json.load(f)
            db.add_participants(wc_participants)
