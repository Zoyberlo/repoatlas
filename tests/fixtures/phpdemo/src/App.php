<?php

declare(strict_types=1);

namespace Demo;

class App
{
    public function build(bool $loud = false): Greeter
    {
        return $loud ? new LoudGreeter(Greeter::DEFAULT_PREFIX) : new Greeter();
    }

    public function run(string $name): string
    {
        $greeter = $this->build(true);

        return $greeter->greet($name);
    }
}
