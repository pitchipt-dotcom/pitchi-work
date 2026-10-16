<?php declare(strict_types=1);
// Isolated API exercise. Only a private copy can receive SQLite journal writes.
copy('/tmp/evidence.sqlite','/tmp/api-link-test.sqlite');
// Historical search snapshots omit optional API tables. Apply the application's
// own idempotent schema to this disposable copy; never migrate the input DB.
$fixtureDb = new PDO('sqlite:/tmp/api-link-test.sqlite', null, null, [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]);
$fixtureDb->exec(file_get_contents('/tmp/project/schema.sql'));
$fixtureDb = null;
putenv('PTOTSEARCH_DB_PATH=/tmp/api-link-test.sqlite');
putenv('PTOTSEARCH_INCLUDE_WORDPRESS_RESULTS=false');
$_SERVER['PATH_INFO']='search';$_SERVER['REQUEST_METHOD']='GET';$_SERVER['HTTP_DNT']='1';
$_GET=['q'=>$argv[1],'limit'=>20];
require '/tmp/project/public/api/index.php';
