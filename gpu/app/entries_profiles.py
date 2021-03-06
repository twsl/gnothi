import pdb, os
import pandas as pd
import numpy as np
import common.models as M
from common.database import session
from ml_tools import Similars, CleanText
from common.fixtures import fixtures
from sqlalchemy import text
from common.utils import vars
from sqlalchemy.sql.expression import func
from app.nlp import nlp_
import logging
logger = logging.getLogger(__name__)


def nlp_on_rows(method='entries'):
    for_entries = method == 'entries'  # else is_profile
    with session() as sess:
        if for_entries:
            rows_ = sess.query(M.Entry) \
                .filter(
                    M.Entry.text.isnot(None),
                    M.Entry.no_ai.isnot(True),
                    M.Entry.ai_ran.isnot(True)
                )
        else:
            rows_ = sess.query(M.User) \
                .filter(
                    M.User.bio.isnot(None),
                    M.User.ai_ran.isnot(True)
                )

        rows = []
        paras_grouped = []
        uids = set()
        for r in rows_.all():
            txt = r.text if for_entries \
                else r.bio  # r.profile_to_text()  # TODO profile_to_text adds people
            paras = len(txt) > 128 and CleanText([txt]).markdown_split_paragraphs().value()
            if not paras:
                # Set everything with not-enough-content to ai_ran, and skip
                if for_entries:
                    r.title_summary = r.text_summary = r.sentiment = None
                r.ai_ran = True
                sess.commit()
                continue
            rows.append(r)
            paras_grouped.append(paras)
            if for_entries:
                uids.add(r.user_id)
        # Everything was too-short of content, nothing to do now.
        if not rows: return {}

        paras_flat = [p for paras in paras_grouped for p in paras]

        fkeys = [r.title for r in rows] \
            if for_entries else [r.email for r in rows]
        fixt = fixtures.load_nlp_rows(fkeys, method=method)
        if fixt:
            if for_entries:
                clean_txt, embeds, titles, texts = fixt
            else:
                clean_txt, embeds = fixt
        else:
            clean_txt = CleanText(paras_flat)\
                .keywords(postags=['NOUN', 'ADJ', 'VERB', 'PROPN'], mode='fast')\
                .join().value()
            # embeds = Similars(paras_flat).embed().autoencode(save_load_path=vars.AE_PATH).value()
            embeds = nlp_.sentence_encode(paras_flat).tolist()
            if for_entries:
                titles = nlp_.summarization(paras_grouped, min_length=5, max_length=20, with_sentiment=False)
                texts = nlp_.summarization(paras_grouped, min_length=30, max_length=250)

        for i, r in enumerate(rows):
            CM = M.CacheEntry if for_entries else M.CacheUser
            c = sess.query(CM).get(r.id)
            if not c:
                c = CM(entry_id=r.id) if for_entries else CM(user_id=r.id)
                sess.add(c)
            # Save the cache_entry (paras,clean,vectors)
            paras = paras_grouped[i]
            c.paras = paras
            ct = len(paras)
            c.clean = clean_txt[:ct]
            c.vectors = embeds[:ct]
            sess.commit()

            # Save the fixture for later
            fixt = (clean_txt[:ct], embeds[:ct], titles[i], texts[i]) \
                if for_entries else (clean_txt[:ct], embeds[:ct])
            fixt_k = r.title if for_entries else r.email
            fixtures.save_nlp_row(fixt_k, fixt, method=method)

            clean_txt, embeds = clean_txt[ct:], embeds[ct:]

            if for_entries:
                r.title_summary = titles[i]["summary"]
                r.text_summary = texts[i]["summary"]
                r.sentiment = texts[i]["sentiment"]
            r.ai_ran = True
            sess.commit()

        if for_entries:
            # 9131155e: only update every x entries
            M.Job.multiple_book_jobs(list(uids))
    return {}


def entries(eid=None):
    return nlp_on_rows('entries')


def profiles(uid=None):
    nlp_on_rows('profiles')
    match_profiles()


def f32(arr):
    # psql loads float64, high-compute
    return np.array(arr).astype(np.float32)

def mean_(vecs):
    if type(vecs) == pd.Series:
        # coming in from pd.groupby.agg. Can't return np.array
        vecs = vecs.dropna()  # TODO why any nans?
        return f32(np.vstack(vecs)).mean(axis=0).tolist()
    return f32(vecs).mean(axis=0)

def match_profiles():
    with session() as sess:
        df = pd.read_sql("""
        select e.user_id, c.vectors from cache_entries c
        inner join entries e on e.id=c.entry_id
        where array_length(c.vectors, 1) > 0
        """, sess.bind)
        if not df.shape[0]: return

        # flatten multi-paragraph entries
        df['vectors'] = df.vectors.apply(mean_)
        # then mean the semantic of all entries for this user.
        # TODO cluster or something, just mean-ing all their entries is stupid
        df = df.groupby(['user_id']).vectors.agg(mean_)

        uids = df.index.tolist()
        vecs_entries = np.vstack(df.values)

        # TODO add community (M.User.public == True)
        df = pd.read_sql("""
        select c.user_id, c.vectors from cache_users c
        inner join users u on c.user_id=u.id 
        where u.therapist=true and c.vectors is not null 
        """, sess.bind)
        if not df.shape[0]: return
        match_ids = df.user_id.tolist()
        # This on the other hand is OK to mean, it's just their profile
        vecs_profiles = np.vstack(df.vectors.apply(mean_).values)

        logger.info(f"Compute distances")
        dists = Similars(vecs_entries, vecs_profiles).normalize().cosine(abs=True).value()

        sess.execute(text("""
        delete from profile_matches where user_id in :uids
        """), dict(uids=tuple(uids)) )
        sess.commit()

        # everything is in same order at this point
        sess.bulk_save_objects([
            M.ProfileMatch(user_id=uid, match_id=mid, score=dists[i,j])
            for i, uid in enumerate(uids)
            for j, mid in enumerate(match_ids)
        ])
        sess.commit()
