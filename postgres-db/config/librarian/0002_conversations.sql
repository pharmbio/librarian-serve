-- Conversations: a question can now follow up on earlier ones. Each query is
-- one turn of a conversation, and its run is the answer to that turn.
--
-- Every query already stored becomes a conversation of its own, with the
-- question as its title. It takes the id of the query's latest run, a
-- completed one if any, so a link to a run from before conversations
-- (/<user id>/<run id>) opens its conversation; a query without a run keeps
-- its own id.

create table conversations (
    id varchar(16) primary key,
    user_id varchar(16) not null references users (id) on delete cascade,
    -- The first question, as asked.
    title text not null,
    created_at timestamp not null
);
create index conversations_by_user on conversations (user_id, created_at);

alter table queries add column conversation_id varchar(16);
update queries q set conversation_id = coalesce(
    (select r.id from runs r where r.query_id = q.id
        order by r.status = 'completed' desc, r.created_at desc, r.id desc limit 1),
    q.id
);
insert into conversations (id, user_id, title, created_at)
    select conversation_id, user_id, text, created_at from queries;
alter table queries
    alter column conversation_id set not null,
    add constraint queries_conversation_id_fkey
        foreign key (conversation_id) references conversations (id) on delete cascade;
create index queries_by_conversation on queries (conversation_id, created_at);

-- How the librarian answered: 'search' (Europe PMC, then a cited answer) or
-- 'reply' (from the conversation alone, no new search). Set when the run
-- completes; every run before conversations searched.
alter table runs add column action varchar(16)
    constraint runs_action check (action in ('search', 'reply'));
update runs set action = 'search' where status = 'completed';

-- The conversations the app lists: those with at least one answered turn,
-- with how many and when the latest was asked. Failed turns don't count, as
-- the app doesn't show them.
create view conversation_summaries as
select
    c.id,
    c.user_id,
    c.title,
    c.created_at,
    count(r.id)::integer as turn_count,
    coalesce(sum(r.paper_count), 0)::integer as paper_count,
    max(r.created_at) as last_turn_at
from conversations c
join queries q on q.conversation_id = c.id
join runs r on r.query_id = q.id and r.status = 'completed'
group by c.id;
